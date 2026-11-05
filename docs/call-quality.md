# Call quality notes

The device has one usable Cortex-A9. It encodes its camera and decodes the computer's camera in the same Chromium 69 process. An unconstrained call worked in both directions and was late: computer-to-device video was clear but delayed, device-to-computer video was choppy and artifacted, and audio was late both ways with usable quality.

## What the pages do now

The device asks its camera for an ideal size and frame rate saved per camera. A new camera starts at 320×240 and 12 frames per second. Echo cancellation and noise suppression start on, and an admin can turn each one off on its own from the Cameras section. The hub sends those values with the device's hello, and again when they are saved. The device uses them the next time it opens the microphone and camera. A call already in progress keeps the old stream.

The computer still requests a modest camera and caps its outgoing video sender at about 200 kbit/s and 12 fps after the answer is built. That cap is on the computer's outbound sender only. The computer's microphone processing is not part of the per-camera settings.

The device page does not rewrite the SDP. It logs the camera track's `readyState` and `getSettings()`, then every few seconds logs outbound RTP bytes. Those lines show up in `journalctl -u petchatz-hub` as `device log:`.

On the computer page the remote picture is muted (so autoplay is allowed) and the device's audio goes to a separate `<audio autoplay>` element. Both `play()` calls run from the Call button click.

After that change, a real call was: video acceptable both ways, audio to the device good, audio from the device delayed but acceptable.

## Changes that made the device go silent

These are recorded so the next edit does not repeat them.

**SDP `b=AS` on the device offer.** Inserting a bandwidth line into the device's local description was accepted by Chromium 69 (an earlier, more aggressive edit that stripped codecs was not: `setLocalDescription` failed with "Invalid SDP line" and the call hung up). With only `b=AS`, ICE still completed and the computer-to-device direction looked excellent, but the device sent no usable audio or video. The computer showed a black video box and no sound. The bandwidth line is gone. The unused `tuneSdp` function is still in the HTML and must not be wired back in.

**`jitterBufferTarget = 0` and `playoutDelayHint = 0` on the computer.** Those were added to cut delay. Current Chrome then played nothing from the device, while Chromium 69 kept playing the computer's media. Removing the properties was not enough by itself, because the `b=AS` offer was still in place. Both are gone now.

**Hard maximum constraints on the device camera.** `max` width, height, and frame rate, plus a second `applyConstraints` with the same max, were removed. The camera is asked for an ideal size and frame rate (the saved values, or 320×240 at 12 fps until an admin changes them) and can fall back to `{audio: true, video: true}`.

## If the device picture disappears again

1. Refresh the computer page so it is not running an old `user.html`.
2. Reload the kiosk with `tools/reload_kiosk.py` so it is not running an old `device.html`.
3. Place a call and read `device log` lines.
   - No camera track, or `readyState` not `live`: the capture constraints are wrong for this sensor.
   - `bytes sent` stays 0 after ICE is `completed`: the device is not transmitting. Suspect SDP changes before suspecting the computer.
   - Bytes increase and the computer is still black: suspect playback (autoplay, a muted element that is also the only place audio was attached, or a jitter-buffer setting).

Do not add a second CPU online to buy headroom. That was tried once and the board rebooted.
