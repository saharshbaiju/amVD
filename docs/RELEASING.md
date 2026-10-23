# amVD — Distribution & releasing

## How users get it

| Piece | Channel | What the user does |
| --- | --- | --- |
| PC side (Python, no build step) | `install.sh` from the `main` branch; downloads the latest release's `amvd-linux.tar.gz` (falls back to the `main` tarball if there's no release yet) | `curl -fsSL https://raw.githubusercontent.com/saharshbaiju/amVD/main/install.sh \| bash`, then `amvd` |
| Phone app | GitHub Releases: `amVD-<version>.apk`, signed with the amVD release key | Download on the phone, install |
| Debug app (testers) | GitHub Releases: `amVD-<version>-debug.apk` — package `com.archextender.debug`, named *Screen Extender (debug)* | Installs *next to* the normal app; for logs and testing |

The installer is distro-agnostic: it asks `linux/core.py` which packages are
missing for *this* distro and desktop (the same list as the TUI's Dependencies
screen) and installs them with apt / dnf / pacman. Nothing is compiled, so the
same tarball works on Ubuntu, Fedora and Arch.

Verified: `install.sh` on Ubuntu 24.04 (fresh container) installs the
packages and the `amvd` command, which then resolves in a new shell. The GNOME
backend streams on Ubuntu 24.04 (Mutter 46, x264enc) and Fedora 42 (Mutter 48,
openh264enc).

## Signing key — keep it safe

Android only installs an update if it's signed with the **same key** as the
installed app. The release key lives outside the repo:

```
~/.config/amvd-release/amvd-release.jks      keystore (alias "amvd")
~/.gradle/gradle.properties                  ASEXT_KEYSTORE / ASEXT_KEYSTORE_PASSWORD / ASEXT_KEY_ALIAS
```

**Back up both** (password manager / encrypted drive). Losing them means every
user has to uninstall before the next version installs. Never commit them.
Without these properties `assembleRelease` falls back to the debug key, which is
fine for local testing but must not be published.

Debug builds use their own package id (`applicationIdSuffix ".debug"`) and the
name *Screen Extender (debug)*, so a debug build and the release app install side
by side and never replace each other. Only an *old* build of
`com.archextender` signed with a different key must be uninstalled once before
the release APK installs: any 0.2 build, and every debug / Android Studio build
made before 0.4.0 (they had no `.debug` suffix, so they took the release
package id with the debug key). On the phone that shows as *"App not
installed — package conflicts with an existing package"*; via adb as
`INSTALL_FAILED_UPDATE_INCOMPATIBLE`. Say so in the release notes.

## Cutting a release

1. Bump `versionCode` (+1) and `versionName` in `android/app/build.gradle.kts`.
2. Build and package:

   ```bash
   V=0.4.1
   (cd android && JAVA_HOME=/opt/android-studio/jbr ./gradlew assembleRelease assembleDebug)
   mkdir -p dist
   cp android/app/build/outputs/apk/release/app-release.apk dist/amVD-$V.apk
   cp android/app/build/outputs/apk/debug/app-debug.apk dist/amVD-$V-debug.apk
   tar --exclude=__pycache__ -czf dist/amvd-linux.tar.gz linux
   (cd dist && sha256sum amVD-$V.apk amVD-$V-debug.apk amvd-linux.tar.gz > SHA256SUMS)
   ```

3. Commit, tag and publish:

   ```bash
   git tag -a v$V -m "amVD $V" && git push origin main v$V
   gh release create v$V dist/amVD-$V.apk dist/amVD-$V-debug.apk \
       dist/amvd-linux.tar.gz dist/SHA256SUMS \
       --title "amVD $V" --notes-file <notes>
   ```

`install.sh` always fetches `releases/latest/download/amvd-linux.tar.gz`, so a
new release is picked up by re-running the install line.

## Possible next channels

- **AUR** package (`amvd-git`) for Arch.
- **F-Droid** for the app (needs a reproducible build without Play-services
  code scanner, or a flavor that drops it).
- **Google Play** (internal testing track) once the app is stable.
- A `.deb` / COPR repo if users want updates through their package manager.
