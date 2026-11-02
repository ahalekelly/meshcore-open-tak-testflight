# MeshCore Open TAK on TestFlight

Builds [jade-on-mesh/meshcore-open-tak](https://github.com/jade-on-mesh/meshcore-open-tak) for iOS and publishes it on TestFlight as `com.akelly.meshcoreopentak`. Join the beta at https://testflight.apple.com/join/TB4jspvT.

- `.github/workflows/testflight.yml` builds an upstream commit with the branches in `patches.txt` merged in, signs it, and uploads it to App Store Connect.
- `scheduler.py` runs every 15 minutes on akelly-desktop (`systemd/`). It starts a build once upstream main has been quiet for 8 hours, rebuilds before the newest build expires, sends each processed build to the Community group for beta review, and opens a T3 thread when a run fails. Its docstring has the details.

Start a build by hand:

```sh
gh workflow run testflight.yml -R ahalekelly/meshcore-open-tak-testflight -f sha=<upstream commit> -f build=$(date -u +%Y%m%d%H)
```

The workflow needs the repo secrets `ASC_KEY_P8` (App Store Connect API key), `DIST_CERT_P12` (base64 Apple Distribution certificate and key) and `DIST_CERT_PASSWORD`. The scheduler reads the same API key from `~/.appstoreconnect/AuthKey_ZT4M83BNFY.p8`.

Install the scheduler on akelly-desktop:

```sh
systemctl --user link ~/Git/meshcore-open-tak-testflight/systemd/meshcore-testflight.{service,timer}
systemctl --user enable --now meshcore-testflight.timer
```
