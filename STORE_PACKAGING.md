# CONTRAconnect — Package for Google Play & Microsoft Store

Your app stays hosted on **Render**. The store apps are thin shells (icons + window) that open your live Render URL. Users install from the store, tap the icon, and work online against Render.

## How it works

```
Phone / PC icon  →  installed app (PWA / TWA / store package)
                         ↓ HTTPS
              https://YOUR-APP.onrender.com  (Flask on Render)
```

No separate mobile backend is required.

---

## 1. Deploy PWA assets (already in this package)

After you deploy this build to Render, verify:

- `https://YOUR-APP.onrender.com/static/manifest.json`
- `https://YOUR-APP.onrender.com/static/js/sw.js`
- Icons under `/static/icons/`

On Android Chrome: menu → **Install app** / **Add to Home screen**.  
On Windows Edge/Chrome: install icon in the address bar.

Set a fixed production URL (custom domain recommended) before store submission.

---

## 2. Google Play Store (Android)

### Option A — Recommended: Trusted Web Activity (TWA) with PWABuilder

1. Open [https://www.pwabuilder.com](https://www.pwabuilder.com)
2. Enter your Render URL → **Start**
3. Fix any PWA checklist items (manifest, icons, HTTPS — you already have these)
4. **Package** → **Android** → download the Android package (Bubblewrap / TWA)
5. Open the project in Android Studio **or** use the generated `.aab`
6. Create a [Google Play Console](https://play.google.com/console) developer account (one-time fee)
7. Create app → upload **Android App Bundle (.aab)**
8. Store listing: title, short/full description, screenshots, **512×512** icon (`static/icons/icon-512.png`)
9. Privacy policy URL (required for health-related apps — host a simple page)
10. Submit for review

### Option B — Bubblewrap CLI (advanced)

```bash
npm i -g @bubblewrap/cli
bubblewrap init --manifest https://YOUR-APP.onrender.com/static/manifest.json
bubblewrap build
```

Digital Asset Links: host `/.well-known/assetlinks.json` on Render so the TWA opens fullscreen without a browser bar. PWABuilder can generate this file.

---

## 3. Microsoft Store (Windows)

1. Open [https://www.pwabuilder.com](https://www.pwabuilder.com) with your Render URL
2. **Package** → **Windows** → generate `.msix` / store package
3. [Partner Center](https://partner.microsoft.com/) account
4. Create app → upload package
5. Use the same branding/icons
6. Submit for certification

Alternatively: Edge → install PWA → “Publish to Microsoft Store” tools from PWABuilder.

---

## 4. Branding assets (included)

| File | Use |
|------|-----|
| `static/icons/icon-512.png` | Play Store high-res icon |
| `static/icons/icon-192.png` | PWA / home screen |
| `static/icons/icon-maskable-512.png` | Android adaptive icon |
| `static/manifest.json` | Name, theme, start URL |

Replace icons with your final logo (keep sizes) when ready.

---

## 5. Important constraints

| Topic | Detail |
|-------|--------|
| **Internet** | App needs network to reach Render (not a full offline ERP) |
| **Render sleep** | Free tier may cold-start; paid instance avoids long first load |
| **HTTPS** | Required for PWA and stores (Render provides this) |
| **Health data** | Play may ask for privacy policy and data-safety form |
| **Updates** | You update the **web** app on Render; store shell rarely needs a new version |
| **Apple App Store** | Possible via PWABuilder / Capacitor, but stricter rules and a Mac/Apple Developer account |

---

## 6. Quick checklist before store submit

- [ ] Custom domain or stable `*.onrender.com` URL
- [ ] HTTPS works; manifest & service worker load
- [ ] “Install” works on Android Chrome and desktop Edge
- [ ] Privacy policy page published
- [ ] Screenshots of login, dashboard, stock, cost analytics
- [ ] Play Console / Partner Center accounts ready

---

## 7. Capacitor (optional native shell)

If you later need camera, push notifications, etc.:

```bash
npm create @capacitor/app
# set server.url to https://YOUR-APP.onrender.com
npx cap add android
npx cap add electron   # or windows
```

For inventory + dashboards only, **PWA + TWA / PWABuilder** is enough and simpler to maintain.
