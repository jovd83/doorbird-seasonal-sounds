# Endpoint discovery — how we found the button-sound upload

> **Superseded — read this first (Aug 2026).** Two claims below turned out to
> be wrong when tested against a real device, and they cost this project
> months:
>
> 1. **The cloud endpoints are not HTTP Basic.** `api.doorbird.io` authenticates
>    with `Authorization: Bearer <device token>`; the `auth:{}` in the bundle is
>    axios boilerplate that a request interceptor overwrites. Basic returns 401.
>    The token comes only from `POST /login` with a mandatory Google reCAPTCHA
>    token, and there is no refresh token for the device scope.
> 2. **These cloud paths do not exist on the device.** `/other/buttonsound[/file]`
>    returns 404 on the LAN, prefixed or not. The bundle contains no `.cgi`
>    references at all — it is a pure cloud client and says nothing about the
>    device's own API.
>
> What is actually true on the LAN: `/bha-api/customsound.cgi` **exists**
> (it answers 401 where a bogus path answers 404) but is gated to the factory
> administration account. The working solution does not use any of this — see
> "How the sound actually reaches the door station" in the top-level README.
>
> The trail below is kept because the mitmproxy addon is still the right way to
> learn `customsound.cgi`'s payload format if anyone gets admin credentials.

(This is a record of how the endpoint was found, kept for traceability and so
the trick can be re-run if DoorBird ever changes the API.)

## The problem

DoorBird's public LAN API (rev 0.36, Nov 2023, `api_lan.pdf`) covers
`info.cgi`, `image.cgi`, `monitor.cgi`, `favorites.cgi`, `schedule.cgi`,
`audio-transmit.cgi`, etc., but **does not document any endpoint to upload
or switch the door station's "Button Sound" custom MP3**. The DoorBird app
does this through a call that's not in the public LAN API.

## The trail

1. DoorBird's [Open API page](https://www.doorbird.com/api) and their FAQ
   page on Windows/macOS access ([faq #55](https://www.doorbird.com/en/faq-single?faq=55))
   point users to two browser-based tools instead of a desktop app:
   - https://webadmin.doorbird.com — the cloud admin SPA
   - https://www.doorbird.com/widget — the live-view widget
2. `webadmin.doorbird.com` is a React SPA. Fetching the HTML reveals one
   compiled JS bundle: `static/js/main.<hash>.js` (about 7.5 MB).
3. Grepping that bundle for `buttonSound`, `buttonsound`, `button_sound`,
   `audio/mpeg`, `Ringtone`, `Chime`, `Melod` lights up dozens of hits.
4. The decisive snippets:

   ```js
   const z3 = "https://api.doorbird.io/";
   // ...
   async uploadLocalMp3File(e, t) {
     if (e.type === "audio/mpeg") {
       const i = { timeout: 45e3, headers: { "Content-Type": "audio/mpeg;charset=UTF-8" } };
       await Rt.post(z3 + "other/buttonsound/file", e, i)
         .then(() => { if (t) this.setButtonSoundOnly("custom"); /* ... */ });
     }
   }

   setButtonSoundOnly(e) {
     await Rt.post(z3 + "other/buttonsound",
                   { buttonSound: e },
                   { "Content-Type": "application/json" });
   }
   ```

   `Rt` is the bundle's name for axios; auth is HTTP Basic with the device
   admin user/password (see `s.set("Authorization","Basic "+btoa(...))` in
   axios's setup code).

5. A bare curl against `https://api.doorbird.io/other/buttonsound` and
   `/data` with no credentials returns `HTTP 401`, confirming the endpoints
   are real and enforce auth.

## What's in this folder

- `captures/webadmin.js` — the SPA bundle, kept so the discovery can be
  re-verified later. **Important: this is DoorBird's intellectual property,
  don't redistribute it.**
- `mitm_addon.py` — the mitmproxy addon written for the *originally*
  planned discovery path (capture from the live app). Not needed any more;
  kept as a fallback if DoorBird ever moves the endpoint and we have to
  re-discover it from app traffic.

## How to refresh the bundle if DoorBird updates the SPA

```bash
HASH=$(curl -sS https://webadmin.doorbird.com/ | grep -oE 'main\.[0-9a-f]+\.js' | head -1)
curl -sS "https://webadmin.doorbird.com/static/js/$HASH" \
  -o discovery/captures/webadmin.js
grep -oE '"buttonSound"|/other/buttonsound[^"]*' discovery/captures/webadmin.js | sort -u
```

If the URLs in the second command change, update the constants at the top
of [`app/doorbird_client.py`](../app/doorbird_client.py).
