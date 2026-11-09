# Calls from outside the house

Two different paths have to leave the house, and they are not the same kind of traffic.

The browser loads `https://<public name>/` and keeps a web socket open. That is TCP port 443 to the hub.

Audio and video do not go through that web socket. Each side learns the other's addresses and tries them. A computer on another network cannot use the device's or the hub's private addresses. The reliable path is the hub's TURN server: both browsers send media to the hub, and the hub forwards it. TURN on this hub is UDP 3478, TCP 3478, and a small UDP relay range, 49160–49200.

The device stays on the private address. Its kiosk keeps opening `https://<lan-ip>/`. That avoids a common router problem where a device inside the house cannot open the house's own public address. Do not change `petchatz_server` to the public name.

```
phone on cellular          home router              proxy                 hub
https://public-name/  ---> TCP 443 ---------------> Zoraxy -- LAN HTTPS -> :443
TURN                  ---> UDP/TCP 3478 ----------------------------------> coturn
media ports           ---> UDP 49160-49200 -------------------------------> coturn
device on the LAN     -------------------------------- stays on the LAN --> :443
```

## Zoraxy

Use Zoraxy for the page and the call socket only. Leave the TURN ports on a router forward straight to the hub. Zoraxy is an HTTP proxy, and the audio and video relay is UDP.

On the hub, the external address is `public_host` in `/opt/petchatz-hub/config.json`. An admin can change it under Outside the house on the Devices page. These docs use `petchatz.example.net` for that name. Inside the house, keep opening `https://<lan-ip>/`. Away from home, open `https://petchatz.example.net/`.

Zoraxy proxy rule:

- Type: HTTP reverse proxy for that hostname. A raw TCP stream would hide the path and break sign-in.
- Upstream: `https://<hub-lan-ip>:443`
- Accept the hub's self-signed certificate. Zoraxy is the name the browser trusts. The hub certificate stays for the device and for browsers on the LAN.
- Leave WebSocket proxying on. The call is a socket at `/signal`.
- Let the original Host header through if Zoraxy has that choice. The page also sends the name it was opened with, and the hub uses the external address only when that name matches the saved one.

Router forwards when Zoraxy is another computer on the LAN:

| Protocol | Ports | Destination |
| --- | --- | --- |
| TCP | 443 | The Zoraxy machine |
| TCP | 3478 | The hub |
| UDP | 3478 | The hub |
| UDP | 49160–49200 | The hub |

The DNS name has to point at the house's public address before Zoraxy can get a certificate for it. The hub's own timer updates the TURN address when the ISP address changes. It does not update the DNS name. The DDNS client still has to do that.

If Zoraxy is running on the hub itself, it and the hub cannot both listen on 443. Give one of them a different port before creating the rule.

## What the hub does

`deploy/enable-offlan.sh` records the public name, sets coturn's `external-ip` to `public/lan` so relay candidates use the address the outside world can reach, and limits relay ports to 49160–49200. A timer rewrites that address if the ISP changes it, and restarts coturn only when it actually changed.

Signed-in browsers that opened the public name receive TURN urls for that name. The device receives TURN urls for the LAN address. Host candidates still win for a call that is already inside the house, so the LAN call does not have to hairpin through the public address.

`/ice.json` requires a login. The TURN password is no longer available to anyone who can open port 443. The device receives its server list on the device socket after it says hello.

A self-signed certificate is generated for the LAN address and the current public address. Browsers will warn. The device already ignores certificate errors. If you later use a domain name, issue a real certificate and save it as `/opt/petchatz-hub/public-cert.pem` and `public-key.pem`. The hub serves that certificate when the browser's name matches `public_host`, and keeps the self-signed certificate for the IP addresses.

## One-time steps

1. On the hub, from a checkout of this repo, pass the external name:

   ```bash
   sudo ./deploy/enable-offlan.sh petchatz.example.net
   ```

   An admin can change that name later under Outside the house on the Devices page. The device is not part of this step.

2. Point the DNS name at the house's public address, and create the Zoraxy rule in the section above. The upstream is the hub's LAN address. A 521 from Zoraxy means it cannot open that upstream. Pointing the rule at the public name loops out through the router and fails the same way. The TURN port forwards are a separate step and do not clear a 521.

3. On the router, forward TURN to the hub and the web port to Zoraxy, as in the table above. Leave port 22 closed. The device's SSH login is not part of a call.

4. Change the admin password from the People page before the web port is open. The hub login will be on the internet.

5. From a phone on cellular, with Wi-Fi off, open `https://petchatz.example.net/`, sign in, and call. The device log should include an `ice relay` line. A LAN call should still use `https://<lan-ip>/`.

Without a proxy, forward TCP 443 to the hub as well and accept the hub's self-signed certificate in the browser. The TURN table does not change.

## A tunnel instead of your own web address

On Devices, under Outside the house, choose A Cloudflare Tunnel. The hostname is still the name people open, and it has to match what the browser sends. In the Cloudflare dashboard, send that hostname to `https://127.0.0.1:443` and turn on No TLS Verify. Paste the tunnel token on the Devices page. The hub stores it in `tunnel.env` and runs `cloudflared` as `petchatz-tunnel`, using HTTP/2 so the tunnel does not need outbound UDP. Install cloudflared from that same page if it is not already on the hub. Saving other settings does not download it.

The tunnel replaces the inbound web-port forward. It does not replace TURN. A call still needs this hub's TURN ports, or Metered, or Cloudflare TURN.

## Metered and Cloudflare TURN

Call relay on the Devices page can use this hub, Metered Open Relay, Cloudflare TURN, or more than one. Leave this hub on for calls inside the house. Add one cloud relay when you do not want to forward the TURN ports. The camera and the browser must both receive that same relay, which the hub does. Host candidates still win a call that is already inside the house.

You can turn a service on and save before you have an account. Calls ignore it until a domain and key, or a Cloudflare key id and token, are saved. The page then shows the last four characters of each secret. The full value stays in `config.json` and is not written to the activity log. Credentials are cached for a while, so a call does not ask the provider again every time someone opens the page. A provider that cannot be reached is left out of the list. The other relays remain.

Cloudflare TURN includes a relay on port 443. That is the path for a mobile network that allows only outbound TCP 443. This hub's own TURN stays off 443, because 443 is the call page.

## A trusted name

Point a DNS name at the public address. For a changing ISP address, use the DNS provider's update client rather than only the hub timer. The timer fixes TURN. It does not update DNS.

Then, on the hub, with port 80 free (nothing in this project listens on 80):

```bash
sudo apt-get install -y certbot
sudo certbot certonly --standalone -d calls.example.com --preferred-challenges http
sudo cp /etc/letsencrypt/live/calls.example.com/fullchain.pem /opt/petchatz-hub/public-cert.pem
sudo cp /etc/letsencrypt/live/calls.example.com/privkey.pem /opt/petchatz-hub/public-key.pem
sudo chmod 600 /opt/petchatz-hub/public-key.pem
sudo systemctl restart petchatz-hub
```

Renewal has to repeat the copy. Those two PEM files are gitignored. Let's Encrypt needs the HTTP challenge to reach this machine, so forward TCP 80 only while issuing or renewing, or leave it forwarded to the hub if port 80 stays closed except for certbot.

## If it still fails from cellular

- Zoraxy returns 521: the proxy cannot connect to its upstream. Use `https://<hub-lan-ip>:443` and accept the hub certificate. Opening the TURN ports does not fix this.
- The page does not load and Zoraxy is not showing its own error: TCP 443 is not forwarded to the proxy, or the ISP blocks inbound 443. A certificate warning means the page did reach a server.
- The page loads, the device is online, and the call stays on "checking": UDP 3478 or 49160–49200 is not forwarded, or coturn's `external-ip` is still the LAN address. `grep ^external-ip= /opt/petchatz-hub/turn.conf` should show `public/lan`.
- The device log shows `ice host` and `ice srflx` but never `ice relay`: the device cannot reach TURN on the LAN address. `systemctl is-active coturn` should be `active`.
- Some mobile networks allow only outbound TCP 443. This hub's own TURN stays off 443, because 443 is the call page. Turn on Cloudflare TURN or Metered under Call relay. Both include a relay on 443, and both sides of the call receive it.

Inside the house, keep using `https://<lan-ip>/`. Opening the public name from inside often fails even when the same name works from cellular.
