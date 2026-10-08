# Emulator harness — reproducible Android emulator bring-up (WhatsApp rail)

`emulator-harness.sh` makes the Android emulator that backs the nosms WhatsApp rail
fully reproducible on **host dq05**. It is a single idempotent script with the
subcommands `preflight | install | create | boot | status | teardown | reap | all`,
plus `--dry-run` and `--help`. Every step prints the exact command it runs, and the
script refuses to run the emulator on any host without an openable `/dev/kvm`.

## The verified E1 facts (do not re-derive — these were established 2026-10-06)

- **dq05** = `c03rad0r-DQ05proplus`, Intel N95, `nproc=4`, 10 GB RAM, 84 GB free on
  `/`, `/dev/kvm` present and openable `O_RDWR` by user `c03rad0r` (member of group
  `kvm`).
- **Android SDK** at `$HOME/Android/Sdk`, with `cmdline-tools/latest` `sdkmanager` 12.0,
  `platforms android-34` + `android-35`, `build-tools 34.0.0`, and licences accepted.
- **`platform-tools/adb` was ALREADY installed** (dated 2025-07-06). Only `emulator`
  (37.2.12) and `system-images;android-34;google_apis_playstore;x86_64` were genuinely
  missing and got installed by `sdkmanager`.
- **The `sdkmanager` "SDK XML version 4 > 3" warning is NON-FATAL.** It must not abort
  the install and must not trigger a cmdline-tools upgrade (sdkmanager 12.0 completed the
  install despite the warning; E1 did not upgrade cmdline-tools).
- **AVD `wa-dev`** (device `pixel_6`, path `~/.android/avd/wa-dev.avd`) was created with
  `avdmanager`. `emulator -accel-check` reported `KVM (version 12) is installed and usable.`
- **Headless boot** reached `sys.boot_completed=1` at `t=10s` (poll `getprop`, never a
  bare sleep). Teardown left no orphan (`pgrep qemu-system/emulator` empty).

## Exact reproduce commands

```bash
# On dq05 (or via ssh dq05). Run from anywhere; paths resolve to $HOME/Android/Sdk.
tools/emulator/emulator-harness.sh --dry-run preflight   # see the gates without executing
tools/emulator/emulator-harness.sh preflight             # all five gates must pass
tools/emulator/emulator-harness.sh install               # sdkmanager install (no cmdline-tools upgrade)
tools/emulator/emulator-harness.sh create                # create AVD wa-dev
tools/emulator/emulator-harness.sh boot                  # headless boot, poll sys.boot_completed
tools/emulator/emulator-harness.sh status                # AVD / adb / process state
tools/emulator/emulator-harness.sh teardown              # stop the guest, preserve userdata
tools/emulator/emulator-harness.sh reap                  # kill orphaned qemu/emulator processes
```

The underlying commands these wrap (from E1, verbatim):

```bash
sdkmanager --sdk_root=$HOME/Android/Sdk platform-tools emulator \
  "system-images;android-34;google_apis_playstore;x86_64"

echo no | avdmanager create avd -n wa-dev \
  -k "system-images;android-34;google_apis_playstore;x86_64" -d pixel_6 --force

$HOME/Android/Sdk/emulator/emulator -avd wa-dev -no-window -no-audio -no-snapshot \
  -gpu swiftshader_indirect -no-metrics -no-boot-anim
```

## The `PlayStore.enabled=no` flag (known, deferred)

`~/.android/avd/wa-dev.avd/config.ini` has `PlayStore.enabled=no` even though the image
is a `google_apis_playstore` build, and `adb root` is refused on this image (expected).
This is a **known flag for the next step** — do not flip it with this harness. The
harness never touches `PlayStore.enabled` and never runs `-wipe-data`.

## Policy: do not leave a 3.3 GB guest on a 4-core box

The guest holds ~3.27 GB RSS (28.5% of the 10 GB box) and pushes load average to ~7.
dq05 is a 4-core box that runs other load. The harness therefore **always teardowns after
a boot** in the normal flow, and `reap` exists to sweep any orphaned `qemu-system` /
`emulator` processes. `teardown` stops the guest via `adb emu kill` but never wipes
userdata.

## What has and has NOT been re-run end-to-end

- **E1 ran this end-to-end on 2026-10-06** (full transcript:
  `tools/emulator/evidence/e1_evidence.log`; preflight: `e1_preflight.log`; host-side
  install + emulator logs: `e1_install.log`, `e1_emu.log`). All four preflight gates
  passed, the toolchain installed, the AVD created, KVM accel confirmed, and the guest
  reached `sys.boot_completed=1` with a live adb shell and no orphaned processes.
- **The live re-run is deferred.** Another worker (card `t_c930a683`) currently holds the
  emulator on dq05 to register a WhatsApp account. Until it releases the emulator, this
  harness must only be exercised on dq05 with `--dry-run` (and read-only checks such as
  `preflight` gates / `emulator -accel-check`). `boot` / `install` / `create` / `teardown`
  / `reap` are implemented and syntax-checked but have NOT been re-exercised live on dq05
  in this landing.
