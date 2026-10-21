# PetTec (Snoop Cube) — csandk Fork for Home Assistant

A minimal Home Assistant integration that adds a **"Feed one portion"** button for the
[PetTec Cam Buddy](https://pettec.de/products/pettec-cam-buddy-inkl-futterautomat) automatic
pet feeder, controlled via the **Snoop Cube** mobile app's cloud API (Meari / CloudEdge).

## What it does

- Logs in to your Snoop Cube / Meari account.
- Discovers any PetTec feeder device on the account (currently the **Cam Buddy**).
- Exposes a single `button` entity per feeder: pressing it dispenses one portion.

That's it. No live video, no sensors, no schedule editor — by design (for v0.1).

## Requirements

- A PetTec Cam Buddy that's already paired in the Snoop Cube app.
- A Meari account that has access to the feeder.

> **Tip:** in the Snoop Cube app, share the feeder with a *second* dedicated email
> address and use that email for HA. Snoop Cube allows only one active session per
> account, and HA can collide with your phone.
