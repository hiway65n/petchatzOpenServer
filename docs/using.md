# Using the hub

Open `https://<hub-ip>/` and accept the certificate warning. That is the sign-in page. After sign-in the browser goes to Calls.

## Pages

- **Calls** lists the cameras this login may use. An admin sees every camera. A user sees only the cameras checked for that account. On a phone, an active call fills the screen, with the camera's picture, a small preview of your own camera, and Hang up along the bottom. On a computer the picture stays at the camera's own pixel size.
- **People** is for an admin. Create an account, choose its cameras, turn it on or off, and set a time when access ends. An empty end time means the account does not expire. Turning an account off, or an end time that has passed, signs that person out. The account you are using cannot be turned off, and at least one admin must stay able to sign in. Anyone can change their own password from Calls. An admin can set a new password for someone else from People, which signs that person out.
- **Devices** is for an admin. Name each camera and set its picture size, frame rate, echo cancellation, and noise suppression. The device uses those values the next time it starts a call. Echo cancellation and noise suppression can be turned off separately. Both start on. Turning echo cancellation off can let the speaker feed back into the microphone. 320 by 240 at 12 frames per second is the starting point these single-core boards can sustain. Remove clears the saved name, settings, and who can call that camera. A camera that is still powered on comes back with the defaults.

Passwords need at least 6 characters. Sessions are cookie tokens stored in `hub.db`. Signing out deletes the cookie on the hub.

The external address, for people outside the house, is saved on the Devices page.

## A call

1. The device must show as online. If it does not, the hub is down, the device JavaScript still points at the old cloud, or the kiosk has not reloaded since that change. See [device.md](device.md).
2. Press Call on that camera. On a phone, stay in the full-screen call. On a computer, stay on the page. The remote picture is a muted video element, and the device's microphone is a separate audio element, so the browser can start playback from the click.
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
