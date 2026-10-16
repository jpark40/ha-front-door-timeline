# Front Door Snapshot Timeline

![Repository icon](icon.png)

Version 1.0.8 prevents an unchanged thumbnail from being saved repeatedly under new timestamps. The optional Eufy SDK capture automation continues to label snapshots from `event.front_door_detection`.

## Install or migrate

1. Back up Home Assistant, including the existing configuration and event history.
2. Add `https://github.com/jpark40/ha-front-door-timeline` in **HACS > three dots > Custom repositories**, type **Integration**. Download it.
3. Keep the existing `front_door_timeline:` block in `configuration.yaml`. For a fresh installation, merge `examples/configuration.yaml` at top level once. Do not paste a duplicate block.
4. Restart Home Assistant. This integration uses YAML setup; no new UI configuration flow is introduced.
5. Manage the matching card through `jpark40/ha-custom-dashboards`. The backend repository does not install JavaScript into `/config/www`.

HACS updates the same `/config/custom_components/front_door_timeline` directory. Do not uninstall the working integration before migrating. Event history remains in the directory configured by your existing YAML, outside the integration source. HACS does not move or delete it.

Existing APIs and service names remain unchanged. For original configuration details, see `LEGACY-INSTALLATION.md`; its manual JS installation instructions are superseded by the shared dashboard bundle.

## Updates and releases

Install an available update through HACS and restart HA. Keep `VERSION` and `custom_components/front_door_timeline/manifest.json` in agreement. Run **Actions > Publish release > Run workflow** on `main` to validate and publish the current version. Increase the version before subsequent releases.

This repository contains one integration, as required by HACS. It is intended to be added as a custom repository; default-catalog submission is separate.

## Front-door capture automation

The optional automation example watches `image.front_door_last_event` and captures that image entity. Replace your prior capture automation only if needed; do not install both. YAML examples are not automatically applied by HACS. The example stores current detection types from `event.front_door_detection` as labels for the Person and Package dashboard tabs. Replace the actions/triggers in your existing automation, keeping its ID. Do not add a duplicate. Unknown or stale detection metadata uses `image_update` and appears in neither the Person nor Package tab. Capture is triggered by a new image timestamp; a detection alone does not mean a new thumbnail is available.

### Repeated thumbnails

An image entity can receive a new timestamp while still returning the previous
picture. Queued captures can also read the same latest thumbnail. The backend
now compares SHA-256 hashes of the image bytes against the latest saved snapshot
with the same label. If unchanged, the capture completes successfully without
adding a file or increasing the counts. This check uses saved history, so it also
works after an HA restart. Different event labels are kept separately to preserve
package and person events; a different image is always saved.

Captures are serialized and written under a temporary filename before an atomic
commit, so the API never indexes a partial snapshot. Existing snapshots are not
deleted or relabeled by the update. Install **v1.0.8** in HACS and restart Home
Assistant; the existing automation can stay as it is.

To inspect skipped captures, temporarily enable info logging for
`custom_components.front_door_timeline`. A skipped capture reports
`Skipped duplicate snapshot ... image bytes are unchanged from ...`. This means
the image source supplied the same data; deduplication cannot make Eufy deliver
a fresh frame. Byte-identical repeats are suppressed; this is not a visual or
perceptual comparison, so recompressed images can still be distinct.

## Icons

The repository contains a custom icon in 256px and 512px PNG, with editable SVG source. For integrations, `brand/icon.png` and `brand/icon@2x.png` are bundled inside the integration; Home Assistant 2026.3+ can display these local brand images.

HACS currently uses its external brands source for integration list icons; the open upstream issue https://github.com/hacs/integration/issues/5223 prevents local-only brand assets from reliably appearing there. Dashboard repositories use HACS's generic Dashboard category icon rather than a per-repository custom icon. The artwork is visible in each repository README and is included for supported HA surfaces. No unsupported icon field is added to hacs.json.

