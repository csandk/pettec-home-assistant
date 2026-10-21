"""Minimal Meari cloud API client used by the PetTec HA integration.

Reverse-engineered from the Snoop Cube Android app (com.pettec.snoopcube).
Auth flow:
  POST /meari/app/login (3DES password, X-Ca-* signed)
  → returns userToken, pfKey.{accessid,accesskey,openapiDomain}, country/phone codes.
Subsequent calls go to the user's regional API (apis-eu-frankfurt.cloudedge360.com)
for device list, and to pfKey.openapiDomain for IoT device-config (the feed cmd).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlencode
from datetime import datetime, timezone

import aiohttp
import pyDes


_LOGGER = logging.getLogger(__name__)


# Snoop Cube AndroidManifest.xml meta-data
APP_KEY = "aTCOXp79xCGLFdb1HfBAPoQadWMdFsIF"
APP_SECRET = "txcAJ2ec+D3nUYRHaXfriovwW9xhUUEC"
SOURCE_APP = 11
APP_VER = "5.10.1"
APP_VER_CODE = 510
DES_KEY = "123456781234567812345678"
DES_IV = "01234567"

DEFAULT_BASE_URL = "https://apis.cloudedge360.com"
RESULT_OK = "1001"
RESULT_LOGGED_ELSEWHERE = "1023"  # "Your account logged in elsewhere..."

# IoT property IDs (from APK IotConstants.java)
#
# Action / write properties
IOT_PROP_PET_FEED2 = "850"        # value: '{"parts": <int>}' to dispense N portions
#
# Read+write toggles / controls
IOT_PROP_CAM_ACTIVE = "118"       # sleepMode tri-state: 1=on, 0=off, 2=privacy
IOT_PROP_RECORDING = "140"        # recordSwitch on/off
IOT_PROP_MOTION_DET = "106"       # motionDetEnable on/off
IOT_PROP_MOTION_SENS = "107"      # motionDetSensitivity 1..N
IOT_PROP_HUMAN_DET = "108"        # humanDetEnable
IOT_PROP_SOUND_DET = "109"        # soundDetEnable
IOT_PROP_SOUND_SENS = "110"       # soundDetSensitivity
IOT_PROP_CRY_DET = "111"          # cryDetEnable
IOT_PROP_HUMAN_TRACK = "112"      # humanTrackEnable (PTZ cameras)
IOT_PROP_PIR_DET = "150"          # PirDetEnable (battery cameras)
IOT_PROP_PIR_SENS = "151"         # PirDetSensitivity
IOT_PROP_PET_ALARM_ENABLE = "264" # petAlarmEnable (feeder)
IOT_PROP_PET_MEOW = "320"         # petMeow on/off (feeder)
#
# Read-only state
IOT_PROP_SD_STATUS = "114"            # int enum: 1=mounted/ok, 3=err, 4=full, 5=unformatted, 6=bad
IOT_PROP_SD_CAPACITY = "115"          # string e.g. "59.463G"
IOT_PROP_SD_REMAINING = "116"         # string e.g. "56.227G"
IOT_PROP_POWER_TYPE = "153"           # 0=battery, 1=mains? (battery devices only)
IOT_PROP_BATTERY_PERCENT = "154"      # int 0-100 (battery devices only)
IOT_PROP_BATTERY_REMAINING = "155"    # int (battery hours/minutes left)
IOT_PROP_CHARGE_STATUS = "156"        # int 0=not charging, 1+=charging
IOT_PROP_WIFI_STRENGTH = "1007"       # int 0-100 (signal percent)
IOT_PROP_FIRMWARE_CODE = "51"         # string firmware identifier
IOT_PROP_FIRMWARE_VERSION = "52"      # string e.g. "6.2.0"
IOT_PROP_FOOD_DET = "331"             # JSON {enable,start_time,stop_time}
IOT_PROP_OUT_FOOD_DET = "337"         # int (minutes feeder has been "out of food")
IOT_PROP_DESICCANT_INFO = "339"       # JSON {expiry_days, status}
IOT_PROP_NEW_TODAY_FEED_PLAN = "344"  # JSON array [{time,count,enable}]
IOT_PROP_FEED_PLAN_LIST = "237"       # JSON array (full week schedule)
IOT_PROP_PET_THROW_WARNING = "236"    # petThrowWarning (feeder bowl tip)

# Properties to fetch for the home dashboard. The /v2/app/iot/model/get/batch
# endpoint accepts ANY props in this list and silently omits ones the device
# doesn't have. Single-batch call for ALL devices, also works for dormant
# battery cameras.
BATCH_READ_PROPS: list[str] = [
    # Power / battery
    IOT_PROP_CAM_ACTIVE, IOT_PROP_POWER_TYPE, IOT_PROP_BATTERY_PERCENT,
    IOT_PROP_BATTERY_REMAINING, IOT_PROP_CHARGE_STATUS, IOT_PROP_WIFI_STRENGTH,
    # SD card
    IOT_PROP_SD_STATUS, IOT_PROP_SD_CAPACITY, IOT_PROP_SD_REMAINING,
    # Firmware
    IOT_PROP_FIRMWARE_CODE, IOT_PROP_FIRMWARE_VERSION,
    # Recording / detection toggles
    IOT_PROP_RECORDING,
    IOT_PROP_MOTION_DET, IOT_PROP_MOTION_SENS,
    IOT_PROP_HUMAN_DET, IOT_PROP_SOUND_DET, IOT_PROP_SOUND_SENS,
    IOT_PROP_CRY_DET, IOT_PROP_HUMAN_TRACK,
    IOT_PROP_PIR_DET, IOT_PROP_PIR_SENS,
    # Pet feeder
    IOT_PROP_PET_THROW_WARNING, IOT_PROP_PET_ALARM_ENABLE, IOT_PROP_PET_MEOW,
    IOT_PROP_FOOD_DET, IOT_PROP_OUT_FOOD_DET, IOT_PROP_DESICCANT_INFO,
    IOT_PROP_NEW_TODAY_FEED_PLAN, IOT_PROP_FEED_PLAN_LIST,
]

# (Kept for backward compat with sensor.py / binary_sensor.py — they use these
# names but receive data from the batch fetch via the coordinator now.)
FEEDER_READ_PROPS = BATCH_READ_PROPS
CAMERA_READ_PROPS = BATCH_READ_PROPS


class MeariAuthError(Exception):
    """Authentication failed (bad credentials)."""


class MeariSessionBumpedError(Exception):
    """Session was invalidated by another login (1023)."""


class MeariApiError(Exception):
    """API call failed for some other reason."""


class DeviceOfflineError(MeariApiError):
    """Device is offline (Meari returned errid=404, reason=NotOnline).

    Common for battery-powered cameras between motion-triggered wake events.
    Callers should mark the entity unavailable rather than fail hard.
    """


# ---- crypto helpers ----------------------------------------------------------

def _triple_des_encrypt(plaintext: str) -> str:
    des = pyDes.triple_des(key=DES_KEY, mode=pyDes.CBC, padmode=pyDes.PAD_PKCS5, IV=DES_IV)
    return base64.b64encode(des.encrypt(plaintext)).decode()


def _hmac_sha1_b64url(message: str, key: str) -> str:
    sig = hmac.new(key.encode(), message.encode(), hashlib.sha1).digest()
    return base64.urlsafe_b64encode(sig).decode()


def _hmac_sha1_b64(message: str, key: str) -> str:
    sig = hmac.new(key.encode(), message.encode(), hashlib.sha1).digest()
    return base64.b64encode(sig).decode()


def _now_ms() -> int:
    return round(time.time() * 1000)


def _sign_headers(api_path: str, key: str, secret: str) -> dict[str, str]:
    """X-Ca-* request headers. For login: APP_KEY/APP_SECRET. For authed: userToken/userToken."""
    timestamp = str(_now_ms())
    nonce = str(random.randint(100000, 999999))
    sign_input = f"api={api_path}|X-Ca-Key={key}|X-Ca-Timestamp={timestamp}|X-Ca-Nonce={nonce}"
    return {
        "X-Ca-Key": key,
        "X-Ca-Nonce": nonce,
        "X-Ca-Sign": _hmac_sha1_b64url(sign_input, secret),
        "X-Ca-Timestamp": timestamp,
        "content-type": "application/x-www-form-urlencoded",
    }


# ---- types -------------------------------------------------------------------

@dataclass
class MeariSession:
    user_id: str
    user_token: str
    country_code: str
    phone_code: str
    access_id: str
    access_key: str
    openapi_domain: str
    base_url: str  # regional API server picked from imageUrl
    raw: dict[str, Any] = field(default_factory=dict)


# ---- client ------------------------------------------------------------------

class MeariClient:
    """Minimal Meari client. Holds one session per email/password."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        email: str,
        password: str,
        country_code: str = "DE",
        phone_code: str = "49",
    ):
        self._http = http
        self._email = email
        self._password = password
        self._country = country_code
        self._phone = phone_code
        self.session: MeariSession | None = None

    async def _post_form(
        self, base_url: str, api_path: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        url = f"{base_url}{api_path}"
        sign_path = "/ppstrongs/" + api_path
        if self.session and self.session.user_token:
            headers = _sign_headers(sign_path, self.session.user_token, self.session.user_token)
            auth_kind = f"userToken={self.session.user_token[:8]}…"
        else:
            headers = _sign_headers(sign_path, APP_KEY, APP_SECRET)
            auth_kind = "APP_KEY"
        body = urlencode(data)
        _LOGGER.debug("PetTec → POST %s  auth=%s  body_len=%d", url, auth_kind, len(body))
        async with self._http.post(url, data=body, headers=headers) as resp:
            text = await resp.text()
            _LOGGER.debug(
                "PetTec ← HTTP %s from %s  body[:300]=%s",
                resp.status, url, text[:300].replace("\n", "\\n"),
            )
            try:
                return json.loads(text)
            except json.JSONDecodeError as err:
                raise MeariApiError(f"non-JSON response from {url}: {text[:200]}") from err

    async def login(self) -> MeariSession:
        body = {
            "phoneType": "a",
            "sourceApp": SOURCE_APP,
            "appVer": APP_VER,
            "iotType": 3,
            "lngType": "en",
            "userAccount": self._email,
            "password": _triple_des_encrypt(self._password),
            "phoneCode": self._phone,
            "appVerCode": APP_VER_CODE,
            "t": str(_now_ms()),
            "countryCode": self._country,
        }
        resp = await self._post_form(DEFAULT_BASE_URL, "/meari/app/login", body)
        if resp.get("resultCode") != RESULT_OK:
            raise MeariAuthError(f"login failed: resultCode={resp.get('resultCode')}")

        result = resp.get("result", {}) or {}
        iot = result.get("iot", {}) or {}
        pfkey = iot.get("pfKey", {}) or {}

        # Pick regional base URL from avatar host (login is global, data APIs are regional)
        avatar = result.get("imageUrl", "") or ""
        base_url = DEFAULT_BASE_URL
        if "apis-" in avatar and ".cloudedge360.com" in avatar:
            host = urlparse(avatar).netloc
            base_url = f"https://{host}"

        self.session = MeariSession(
            user_id=str(result.get("userID", "")),
            user_token=result.get("userToken", "") or "",
            country_code=result.get("countryCode") or self._country,
            phone_code=result.get("phoneCode") or self._phone,
            access_id=pfkey.get("accessid", "") or "",
            access_key=pfkey.get("accesskey", "") or "",
            openapi_domain=pfkey.get("openapiDomain", "") or "",
            base_url=base_url,
            raw=resp,
        )
        if not (self.session.user_token and self.session.access_key and self.session.openapi_domain):
            raise MeariAuthError("login response missing token / pfKey")
        return self.session

    def _signed_body(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """RequestParams(5) body — body-signed with userToken as HMAC key."""
        if not self.session:
            raise MeariApiError("not logged in")
        ts_ms = _now_ms()
        iso_ts = (
            datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S."
            )
            + f"{ts_ms % 1000:03d}UTC"
        )
        params: dict[str, Any] = {
            "phoneType": "a",
            "appVer": APP_VER,
            "appVerCode": str(APP_VER_CODE),
            "lngType": "en",
            "t": str(ts_ms),
            "sourceApp": str(SOURCE_APP),
            "userID": self.session.user_id,
            "countryCode": self.session.country_code,
            "phoneCode": self.session.phone_code,
            "signatureMethod": "HMAC-SHA1",
            "timestamp": iso_ts,
            "signatureVersion": "1.0",
            "signatureNonce": str(ts_ms),
        }
        if extra:
            params.update({k: str(v) for k, v in extra.items()})

        canonical = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
        params["signature"] = _hmac_sha1_b64(canonical, self.session.user_token)
        return params

    async def get_device_list(self) -> dict[str, Any]:
        """Returns {nvr: [...], ipc: [...], snap: [...], chime: [...]}.

        NOTE: the decompiled SDK passes deviceTypeId=2 here, but doing so
        makes the server return 1023 ("logged in elsewhere"). Empirically the
        server only accepts this call WITHOUT that field. Likely the SDK
        constant drifted from what the live server expects.
        """
        if not self.session:
            raise MeariApiError("not logged in")
        resp = await self._post_form(
            self.session.base_url,
            "/v1/app/device/info/get",
            self._signed_body(),
        )
        rc = resp.get("resultCode")
        if rc == RESULT_OK:
            return resp
        if rc == RESULT_LOGGED_ELSEWHERE:
            raise MeariSessionBumpedError(f"device list: resultCode={rc} (logged in elsewhere)")
        raise MeariApiError(f"device list failed: resultCode={rc}")

    # ---- IoT openapi (device control) ----------------------------------------
    #
    # IoT control goes through a different endpoint than the cloud REST API:
    # GET {pfKey.openapiDomain}/openapi/device/config?accessid=…&expires=…
    #     &signature=…&action=set&deviceid=<formatSn(snNum)>&params=<base64 JSON>
    #
    # Signature: base64(HMAC-SHA1("GET\n\n\n{expires}\n{path}\n{action}", access_key))
    # formatSn:  9-char SN → "0"+sn ; else SN[4:]  (PetTec UIDs are 20 chars: drop "ppsl"/"ppsc"/"ppil"/etc prefix)

    @staticmethod
    def _format_sn(sn: str) -> str:
        if not sn:
            return ""
        if len(sn) == 9:
            return "0" + sn
        return sn[4:]

    @staticmethod
    def _expires_str() -> str:
        """Match SdkUtils.getTimeOut(): now + 60s, then subtract local TZ offset."""
        now_ms = _now_ms() + 60_000
        local = time.localtime(now_ms / 1000)
        tz_offset_ms = local.tm_gmtoff * 1000
        adjusted_ms = now_ms - tz_offset_ms
        return str(adjusted_ms // 1000)

    @staticmethod
    def _build_iot_params_b64(action: str, iot_payload) -> str:
        """Build the base64-encoded params blob.

        action='set': iot_payload should be a dict {prop_id: value}.
        action='get': iot_payload should be a list [prop_id, ...].
        """
        body = {
            "code": 100001,
            "action": action,
            "name": "iot",
            "iot": iot_payload,
        }
        return base64.b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()

    async def _iot_request(
        self, sn_num: str, action: str, iot_payload
    ) -> dict[str, Any]:
        """Shared GET request to /openapi/device/config for both set and get."""
        if not self.session:
            raise MeariApiError("not logged in")
        path = "/openapi/device/config"
        expires = self._expires_str()
        sign_input = f"GET\n\n\n{expires}\n{path}\n{action}"
        signature = _hmac_sha1_b64(sign_input, self.session.access_key)
        query = {
            "accessid": self.session.access_id,
            "expires": expires,
            "signature": signature,
            "action": action,
            "deviceid": self._format_sn(sn_num),
            "params": self._build_iot_params_b64(action, iot_payload),
        }
        url = self.session.openapi_domain.rstrip("/") + path
        _LOGGER.debug("PetTec IoT → %s %s payload=%s", action, sn_num, iot_payload)
        async with self._http.get(url, params=query) as resp:
            text = await resp.text()
            _LOGGER.debug("PetTec IoT ← HTTP %s body=%s", resp.status, text[:300])
            try:
                data = json.loads(text)
            except json.JSONDecodeError as err:
                raise MeariApiError(
                    f"non-JSON response from {url}: {text[:200]}"
                ) from err
            return data

    async def set_iot_property(
        self, sn_num: str, props: dict[str, Any]
    ) -> dict[str, Any]:
        """Write IoT properties on a device. props: {prop_id: value}.

        Value types matter — the server accepts strings but silently ignores
        them for primitive props (sleepMode, recording, etc.). Pass INTS for
        primitive on/off and number values; pass STRINGS only for props whose
        values are JSON sub-objects (e.g. petFeed2 takes '{"parts":1}').
        """
        data = await self._iot_request(sn_num, "set", props)
        if isinstance(data, dict) and data.get("errid"):
            raise MeariApiError(f"device config set failed: {data}")
        return data

    async def get_iot_properties(
        self, sn_num: str, prop_ids: list[str]
    ) -> dict[str, Any]:
        """Read IoT properties on a device. Returns a dict {prop_id: value}.

        Quirks:
        - The server SILENTLY drops properties the device doesn't track. Always
          handle missing keys via .get(prop, default) downstream.
        - When the device is offline (esp. battery cams), the server returns
          {"errid": 404, "reason": "NotOnline", ...}. We surface that as a
          DeviceOfflineError so callers can mark entities unavailable.
        """
        data = await self._iot_request(sn_num, "get", prop_ids)
        if isinstance(data, dict) and data.get("errid"):
            err = data.get("errid")
            reason = data.get("reason", "")
            if err == 404 and reason == "NotOnline":
                raise DeviceOfflineError(f"{sn_num} is offline")
            raise MeariApiError(f"device config get failed: {data}")
        # Successful shape: {"code":..., "action":"get", "name":"iot", "iot":{...}}
        iot = data.get("iot") if isinstance(data, dict) else None
        if not isinstance(iot, dict):
            return {}
        return iot

    # ---- camera control (DP 118) --------------------------------------------
    #
    # Tri-state: 1=on, 0=off, 2=privacy. v0.2 only flips on/off; privacy is
    # surfaced through `_state_value_is_active()` so a future select entity
    # can map all three values cleanly.

    @staticmethod
    def _state_value_is_active(value) -> bool | None:
        """Map prop 118 (sleepMode) raw value to whether camera is ACTIVE.

        Empirically verified by toggling cameras in the Snoop Cube app:
        - 0 → sleep mode OFF → camera is **active** → True
        - 1 → sleep mode ON  → camera is **inactive** → False
        - 2 → privacy mode   → camera is **inactive** → False
        - anything else / None → None (unknown)

        Note the inversion: prop 118 reads "is sleep mode enabled?", so
        active==True corresponds to value==0.
        """
        if value is None:
            return None
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None
        if v == 0:
            return True
        if v in (1, 2):
            return False
        return None

    async def set_camera_active(self, sn_num: str, on: bool) -> dict[str, Any]:
        """Enable (active) or disable a camera (writes inverted DP 118).

        Active==True writes 0 (sleep mode off). Active==False writes 1
        (sleep mode on). Privacy mode (2) is not exposed by this method;
        a future v0.3 select entity will write that value when needed.
        """
        return await self.set_iot_property(
            sn_num, {IOT_PROP_CAM_ACTIVE: 0 if on else 1}
        )

    async def set_toggle(self, sn_num: str, prop_id: str, on: bool) -> dict[str, Any]:
        """Generic on/off toggle of an IoT property. Sends int 1/0."""
        return await self.set_iot_property(sn_num, {prop_id: 1 if on else 0})

    async def set_number(self, sn_num: str, prop_id: str, value: int) -> dict[str, Any]:
        """Generic integer write to an IoT property."""
        return await self.set_iot_property(sn_num, {prop_id: int(value)})

    async def get_iot_batch(
        self, sn_list: list[str], prop_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Batch-fetch IoT properties for multiple devices in one cloud call.

        This hits the regional API server (NOT the openapi domain). It's the
        same endpoint the Snoop Cube home view uses. Critically, **it works
        even for dormant battery cameras** — the openapi /device/config call
        returns errid=404 for them, but this one returns cached state
        including battery/wifi.

        Returns: {sn_with_full_prefix_dropped: {prop_id: value, ...}, ...}
        """
        if not self.session:
            raise MeariApiError("not logged in")
        # snIdentifier = {"<full_sn>": "comma,separated,prop,ids", ...}
        # The endpoint expects FULL SNs (with "ppsl"/"ppsc" prefix), not the
        # stripped form used by the openapi /device/config endpoint.
        prop_csv = ",".join(prop_ids)
        sn_identifier = {sn: prop_csv for sn in sn_list}
        body = self._signed_body({
            "snIdentifier": json.dumps(sn_identifier, separators=(",", ":"))
        })
        api_path = "/v2/app/iot/model/get/batch"
        sign_path = "/ppstrongs" + api_path
        if self.session.user_token:
            headers = _sign_headers(
                sign_path, self.session.user_token, self.session.user_token
            )
        else:
            headers = _sign_headers(sign_path, APP_KEY, APP_SECRET)
        url = self.session.base_url + api_path
        _LOGGER.debug("PetTec batch → %s for %d devices", url, len(sn_list))
        async with self._http.get(url, params=body, headers=headers) as resp:
            text = await resp.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError as err:
                raise MeariApiError(f"non-JSON batch response: {text[:200]}") from err
        rc = data.get("resultCode") if isinstance(data, dict) else None
        if rc != RESULT_OK:
            if rc == RESULT_LOGGED_ELSEWHERE:
                raise MeariSessionBumpedError(f"batch IoT: resultCode={rc}")
            raise MeariApiError(f"batch IoT failed: resultCode={rc}")
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            return {}
        return {sn: props for sn, props in result.items() if isinstance(props, dict)}

    async def wake_device(self, sn_num: str) -> dict[str, Any]:
        """Wake a dormant battery camera.

        Path: GET /openapi/device/awaken?action=set&deviceid=<formatSn>&sid=...

        Sends a wake-up command via the cloud. The camera responds within a
        few seconds (usually <2s) and transitions from dormancy → online.
        Required before issuing any IoT write to a sleeping battery cam,
        because /openapi/device/config returns 404 NotOnline for dormant
        devices.
        """
        if not self.session:
            raise MeariApiError("not logged in")
        path = "/openapi/device/awaken"
        action = "set"
        expires = self._expires_str()
        sign_input = f"GET\n\n\n{expires}\n{path}\n{action}"
        signature = _hmac_sha1_b64(sign_input, self.session.access_key)
        formatted_sn = self._format_sn(sn_num)
        # sid: device id + millis, max 30 chars
        sid = (formatted_sn + str(_now_ms()))[:30]
        query = {
            "accessid": self.session.access_id,
            "expires": expires,
            "signature": signature,
            "action": action,
            "deviceid": formatted_sn,
            "sid": sid,
        }
        url = self.session.openapi_domain.rstrip("/") + path
        _LOGGER.info("PetTec: wake %s", sn_num)
        async with self._http.get(url, params=query) as resp:
            text = await resp.text()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_raw": text}

    async def get_device_status(self, sn_num: str) -> str:
        """Liveness check via /openapi/device/status?action=query.

        Returns one of:
          - "online"    — fully reachable
          - "dormancy"  — battery cam in low-power sleep (still controllable)
          - "offline"   — Meari can't reach the device (powered off / Wi-Fi loss)
          - "notfound"  — device unknown or deregistered
          - "unknown"   — couldn't parse response

        This is a separate cloud endpoint from the IoT openapi we use for
        get/set props. The IoT openapi can return a *cached* property value
        (esp. config-only props like 118) even when the camera itself is
        unreachable, so it's NOT a reliable liveness signal on its own.
        """
        if not self.session:
            raise MeariApiError("not logged in")
        path = "/openapi/device/status"
        action = "query"
        expires = self._expires_str()
        sign_input = f"GET\n\n\n{expires}\n{path}\n{action}"
        signature = _hmac_sha1_b64(sign_input, self.session.access_key)
        query = {
            "accessid": self.session.access_id,
            "expires": expires,
            "signature": signature,
            "action": action,
            "deviceid": self._format_sn(sn_num),
        }
        url = self.session.openapi_domain.rstrip("/") + path
        async with self._http.get(url, params=query) as resp:
            text = await resp.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                _LOGGER.debug("PetTec status non-JSON: %s", text[:120])
                return "unknown"
        if not isinstance(data, dict):
            return "unknown"
        if data.get("errid"):
            return "offline"
        status = data.get("status")
        if isinstance(status, str):
            return status
        return "unknown"

    async def feed_one_portion(self, sn_num: str, portions: int = 1) -> dict[str, Any]:
        """Trigger N portions on a Cam Buddy (or compatible feeder)."""
        return await self.set_iot_property(
            sn_num,
            {IOT_PROP_PET_FEED2: json.dumps({"parts": portions}, separators=(",", ":"))},
        )

    async def list_devices_with_retry(self, retries: int = 5) -> list[dict[str, Any]]:
        """list_devices() with automatic re-login on 1023 (session bumped).

        Meari only allows ONE active session per account. Any second login
        from anywhere — phone, second HA instance, the Snoop Cube web UI —
        invalidates ours. We re-login between attempts; whoever logs in last
        wins. With a small backoff between retries, we usually win within a
        couple of tries unless something is _actively_ logging in.
        """
        import asyncio as _asyncio
        for attempt in range(retries + 1):
            try:
                return await self.list_devices()
            except MeariSessionBumpedError:
                if attempt == retries:
                    raise
                _LOGGER.info(
                    "PetTec session bumped (attempt %d/%d) — re-logging in",
                    attempt + 1,
                    retries,
                )
                await _asyncio.sleep(0.5 + attempt * 0.5)
                await self.login()
        return []  # unreachable

    async def list_feeders_with_retry(self, retries: int = 5) -> list[dict[str, Any]]:
        """Return only feeders, with the same session recovery as discovery."""
        return [
            device for device in await self.list_devices_with_retry(retries)
            if self.is_feeder(device)
        ]

    # ---- helpers: enumerate cameras and feeders -----------------------------

    @staticmethod
    def is_feeder(device: dict[str, Any]) -> bool:
        """True if device capabilities indicate a feeder (pet:N or pfp:1)."""
        cap_str = device.get("capability") or "{}"
        try:
            cap = json.loads(cap_str)
        except (TypeError, ValueError):
            return False
        caps = cap.get("caps", {}) or {}
        return bool(caps.get("pet")) or bool(caps.get("pfp"))

    async def list_devices(self) -> list[dict[str, Any]]:
        """Return all cameras and feeders from both device-list buckets."""
        resp = await self.get_device_list()
        out: list[dict[str, Any]] = []
        for bucket in ("ipc", "snap"):
            out.extend(resp.get(bucket) or [])
        return out

    async def list_feeders(self) -> list[dict[str, Any]]:
        """Return all camera devices with feeder capabilities."""
        return [device for device in await self.list_devices() if self.is_feeder(device)]
