# Using the hub

Open `https://<hub-ip>/` and accept the certificate warning.

## Accounts

The installer creates one admin. An admin sees every camera that has connected and can call any of them. From the People section an admin creates accounts, sets a new password for any account, and checks which cameras a user may call. A user can have more than one camera. Clearing every camera and saving removes that person's access. An admin account is not limited by those checkboxes.

Passwords need at least 6 characters. Anyone signed in can change their own password from the page, and that form asks for the current password. When an admin sets someone else's password, that person is signed out and must use the new one. The picture in a call is shown at the camera's own pixel size, and it shrinks to fit a phone screen.

An admin names each camera and sets its picture size, frame rate, echo cancellation, and noise suppression from the Cameras section. The device uses those values the next time it starts a call. Echo cancellation and noise suppression can be turned off separately when testing the microphone. Both start on. Turning echo cancellation off can let the speaker feed back into the microphone, because they sit next to each other. 320 by 240 at 12 frames per second is the starting point that these single-core boards can sustain. A higher frame rate is smoother until the device falls behind. The name is what people see on the call list. The device id stays on the card so it can still be assigned to an account.

Sessions are cookie tokens stored in `hub.db`. Signing out deletes the cookie on the hub.

## A call

1. The device must show as online. If it does not, the hub is down, the device JavaScript still points at the old cloud, or the kiosk has not reloaded since that change. See [device.md](device.md).
2. Press Call on that device. Stay on the page. The remote picture is a muted video element, and the device's microphone is a separate audio element, so the browser can start playback from the click.
3. The status line moves through the ICE state (`checking`, `connected`, `completed`) and also reports when a remote audio or video track arrives.
4. Hang up from the page. Closing the tab also ends the call.

One call at a time per device. A second caller gets an error until the first call ends.

## What "online" means

The device page opens `wss://<hub>/signal?role=device` and sends a hello with its device id, version, and hardware string. The hub keeps that in memory. A reboot of the hub forgets who is online until the device connects again. Users, assignments, sessions, and camera settings are in SQLite and survive a restart.

## Logs

```bash
journalctl -u petchatz-hub -f
```

Useful lines are `device online`, `call ... user ... device ...`, `device log:` (camera settings and bytes sent), and ICE state from the device. Bytes sent growing means the device is transmitting. A black picture in the computer browser with growing bytes is a playback or decoding problem on the computer. Bytes stuck at zero means the device is not sending.

`tools/callcheck.py` logs in, asks the device for an offer, prints a few SDP lines, and hangs up. It does not answer, so it will not show audio or video. Pass the hub URL, admin user, password, and device id in the environment. Do not write those into the script.
