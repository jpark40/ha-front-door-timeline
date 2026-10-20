# Front Door Snapshot Timeline

![Repository icon](icon.png)

Version 1.0.10 fixes Refresh source: it now saves the available image directly to the timeline, waits for a background image update, and reports clearly when no newer or classifiable image is available. Earlier delayed-image handling and duplicate protection are retained.

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

### Eufy SDK compatibility in 1.0.9

Reviewed against the stable **eufy-sdk integration 0.6.0**, **bridge 0.6.0**,
and **bridge add-on 0.7.0** released October 16, 2026. These are separate version
numbers; see the [integration release](https://github.com/mega-yfue/ha-eufy-sdk/releases/tag/0.6.0)
and [add-on release](https://github.com/mega-yfue/ha-eufy-sdk-addon/releases/tag/0.7.0).

The bridge can retry a local HomeBase cover for **240 seconds**. The old example's
60-second label window could save that valid late image as `image_update`, which
the Person/Package tabs hide. When an existing automation supplies `image_update`
for an SDK `image.*_last_event`, the backend now checks the corresponding
`event.*_detection` timestamp and accepts a known Person/Package label within
300 seconds (plus a five-second reverse-order allowance). It uses the image's
timestamp rather than the time a queued capture executes. Older detections and
unknown types are not used. Explicit capture labels retain their meaning.

The existing automation can stay in place. For new installations the example
also uses the 300-second bound. The image and detection entity names must follow
the documented SDK naming pair; renamed pairs need the explicit automation label.
Historical `image_update` snapshots cannot be classified reliably without their
original detection metadata and are not automatically relabeled.

Update **both** this integration and `ha-custom-dashboards` to **1.0.9**, restart
HA, and fully reload each dashboard/Fully Kiosk page. The card now follows new
dates and frames, and shows a warning when Eufy reports detections but its event
image remains older by more than five minutes.

To request a fresh stored event cover through the SDK's supported button, run:

```yaml
action: front_door_timeline.refresh_source
data:
  entity_id: image.front_door_last_event
```

In **v1.0.10**, the action resolves the matching button by registry unique ID,
requests a refresh, and archives the currently available classified image itself.
It does not depend on the image automation firing a new state-change trigger.
The saved filename uses the **source image timestamp**, so syncing an old cover
does not make it look like a new event. Identical content is still suppressed.
After the button returns, the action waits up to 20 seconds for a background
image update if the existing image is already archived or unavailable. A saved
image response contains its filename, label, and source timestamp. If nothing
new arrives, the action raises a clear error containing the source timestamp and
the existing filename instead of returning a silent success.

If no Person/Package detection can be matched to the source image, the action
adds no hidden `image_update` entry and reports the classification problem. After
visually checking the source image, you can explicitly provide `label: person`
or `label: package` to sync that verified category.

Install backend **v1.0.10**, keep dashboard **v1.0.9 or later**, restart HA, and run
the action again. Regular automatic capture still uses the existing automation.
This action does not start a live stream. It cannot force a HomeBase to return a cover it has not
provided. The bridge's [current HomeBase 3 report](https://github.com/mega-yfue/ha-eufy-sdk-bridge/issues/134)
and [troubleshooting guide](https://github.com/mega-yfue/ha-eufy-sdk-bridge/blob/main/docs/troubleshooting.md)
describe cached thumbnails, missing local covers, and push/DNS failures. If the
source stays stale, inspect bridge log entries containing `/event-image`,
`local refresh`, `force refresh`, or `cover reply`; a `200 cached thumbnail` is
not confirmation that a fresh frame arrived.

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

