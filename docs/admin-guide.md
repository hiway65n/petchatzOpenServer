# Admin guide

This is the full guide for an admin. On the hub, Help includes this only while an admin is signed in. A caller, or someone who is not signed in, receives [guide.md](guide.md) and does not receive these sections. Install, the camera, and the network are in [install.md](install.md), [device.md](device.md), and [off-lan.md](off-lan.md).

## Calls

The caller steps are in [guide.md](guide.md). An admin can call every camera, not only the ones assigned to one person. Mute and Camera off change the admin's own microphone and camera. The pet camera does not flip or turn off.

## People

Create an account, check the cameras that person can call, turn the account on or off, and set when access ends. An empty end time means it does not expire. Turning an account off, deleting it, or an end time that has passed signs that person out and ends a call they were in.

Delete account removes that person. The account you are signed in with cannot be deleted, and at least one admin who can still sign in has to remain.

A user cannot change a password. Set password, on that person's card, is how you change it. That signs them out. My password, at the bottom, is where you change the password for the admin account you are using. Type the current password first. Random password fills in 8 characters and shows them so you can copy them.

Guest account is a short wizard. Choose one camera, the date access ends, and an email if you want one. The hub makes a username and password and opens a page you can print. If mail is saved on Settings, the same page is emailed. Those guest accounts are the only ones the cleanup task on Settings removes. Accounts you create with New account stay until you delete them.

## Devices

Name each camera and set its picture size, frame rate, echo cancellation, and noise suppression. The line under a camera is its id and the last address it used to reach the hub.

Deploy to this camera uses that camera's address on the home network. It installs the watchdog change, points the camera at the hub, and reloads its screen. The camera must still use the empty root password it shipped with. A camera that is already set up is left running.

Treat dispenser and Scent start off. Turn one on and save, and Calls shows that button for that camera. One scent press runs the fan for about 10 seconds.

Camera screen sets two pictures used by every camera: one while it is ready, and one while it is outside its hours. The panel is 320 by 240. A larger picture is scaled and cropped to that panel. With no picture, the screen keeps the Ready or Out of service words.

Remove clears the saved name, settings, and who can call that camera. A camera that is still on comes back with the defaults.

## Schedule

Set the house time zone, when each camera can be used, and how many treats and scent releases are allowed in 24 hours. With no windows, a camera can be used any time. A "can be used" window limits it to those times. A "cannot be used" window blocks it, including inside an open window. Windows can repeat every week, or apply to one date. A window that ends earlier than it starts continues through midnight.

People who can call that camera are told when the next open time starts. The camera screen says Out of service during a closed window and Ready when it opens. A picture from Devices replaces that text. A call already underway is left in place. Treat and scent stay off outside an open window. Each camera allows 12 treats and 12 scent releases in 24 hours until you change that number.

## Activity

This is the record of sign-ins, calls, treats, scent, and changes. Filter by a username, a camera, or both. Each entry includes the address the person connected from. Passwords are never written here. The newest 10,000 entries are kept.

## Settings

Name is the title on the pages, on the guest printout, and in email. Leave it blank to keep OpenPetChatz. The top of each page can show that name, a logo picture, or both.

Guest instructions are the text of the printed page and the email. `{name}`, `{logo}`, `{camera}`, `{expires}`, `{address}`, `{username}`, and `{password}` are filled in for each guest. `{logo}` is the saved picture. The text has to keep `{username}` and `{password}`. Restore the starting text puts the original wording back in the box. Save to keep it.

How people open the page is either a web address you publish or a Cloudflare Tunnel. The tunnel carries the page only. Calls still need a relay. Leave this hub's relay on for calls inside the house, and add one cloud relay when callers are away. Keys and the tunnel token are stored on the hub. After one is saved, the page shows only its last four characters.

Mail is the server used to email a guest sign-in and, if you turn it on, activity. Activity mail can be off, errors only, or every activity. Those messages do not include passwords. The guest email does include the new password. Send a test checks the server.

Tasks run once a day at the time you choose, in the house time zone. Delete expired guest accounts removes guest sign-ins after their end date. It is off until you turn it on. Run now does it immediately. Other accounts are not removed by that task.
