# Front Door Snapshot Timeline

![Repository icon](icon.png)

Version 1.0.13 adds a separate live-video source when Eufy's Last event image stops updating. For a new Person/Package detection, it first tries the event image, then decodes one frame from the matching camera's video stream if needed. It bypasses the SDK's normal camera snapshot endpoint, which can itself return an old stored picture. Existing history, categories, and duplicate protection remain available.

## Install or migrate

1. Back up Home Assistant, including the existing configuration and event history.
2. Add `https://github.com/jpark40/ha-front-door-timeline` in **HACS > three dots > Custom repositories**, type **Integration**. Download it.
3. Keep the existing `front_door_timeline:` block in `configuration.yaml`. For a fresh installation, merge `examples/configuration.yaml` at top level once. Do not paste a duplicate block.
4. Turn off the old **Front Door - Save snapshots to timeline** automation. Automatic capture is enabled by default in 1.0.12, so that automation is no longer needed.
5. Restart Home Assistant. This integration uses YAML setup; no new UI configuration flow is introduced.
6. Update `jpark40/ha-custom-dashboards` to **1.0.12** and fully reload each browser/Fully Kiosk page. The backend repository does not install JavaScript into `/config/www`.

HACS updates the same `/config/custom_components/front_door_timeline` directory. Do not uninstall the working integration before migrating. Event history remains in the directory configured by your existing YAML, outside the integration source. HACS does not move or delete it.

Existing APIs and service names remain unchanged. For original configuration details, see `LEGACY-INSTALLATION.md`; its manual JS installation instructions are superseded by the shared dashboard bundle.

## Updates and releases

Install an available update through HACS and restart HA. Keep `VERSION` and `custom_components/front_door_timeline/manifest.json` in agreement. Run **Actions > Publish release > Run workflow** on `main` to validate and publish the current version. Increase the version before subsequent releases.

This repository contains one integration, as required by HACS. It is intended to be added as a custom repository; default-catalog submission is separate.

## Front-door capture automation

### Separate live-video fallback in 1.0.13

The default fallback uses the camera with the same Eufy serial as
`image.front_door_last_event`, including a renamed entity such as
`camera.front_door_2`. No YAML change is normally necessary. If you want to
choose a different live-video camera explicitly, merge these settings into
your existing block:

```yaml
front_door_timeline:
  directory: /media/front_door_snapshots
  retention_days: 30
  auto_capture: true
  source_entity: image.front_door_last_event
  live_fallback: true
  fallback_camera: camera.front_door_2
```

After trying the SDK refresh and allowing two seconds for an event image,
the integration opens the selected camera's live video source through FFmpeg
and saves one JPEG frame. It never calls `camera.snapshot`, changes Eufy's
snapshot policy, or accepts `/snapshot`'s cached fallback. The FFmpeg
integration dependency loads automatically; a separate `ffmpeg:` block is
not required on HAOS.

The live request has a 30-second timeout and its decoder/consumer closes on
success, failure, or HA shutdown. It can briefly wake a battery camera, once
per eligible detection; it does not preload a continuous HA stream. Set
`live_fallback: false` to use event images only. Set `fallback_camera` to
another HA camera with a working RTSP/HTTP video stream to use that source
instead of the matched Eufy camera.

A live frame shows the scene **after detection**, not a recovered historical
event thumbnail. Both the event time and actual frame acquisition time appear
in diagnostics. Live frames older than 60 seconds after detection are rejected,
and a restored detection from yesterday never triggers a new live capture.
A newer detection or a primary event image arriving during video startup
causes the superseded live result to be discarded. A failed attempt leaves
the pending event eligible for a late primary image.

`source.fallback` in diagnostics identifies the selected camera and method;
`source.capture` reports `image_source: live_stream`, `camera_entity`, and
`captured_at` after success, or `fallback_error` after failure. Dashboard
1.0.12 shows these states and avoids warning about the old event image when
a new live frame was saved. Live viewing must work on the selected camera;
this cannot repair a camera or bridge that supplies neither events nor video.

### Automatic detection capture in 1.0.12

Normal operation no longer needs a capture automation. With your existing
configuration block, the integration watches `image.front_door_last_event` and
resolves the matching Eufy SDK detection entity through registry unique IDs.
Optional settings are:

```yaml
front_door_timeline:
  directory: /media/front_door_snapshots
  retention_days: 30
  auto_capture: true
  source_entity: image.front_door_last_event
```

Merge settings into the existing block. A renamed source can be specified with
`source_entity`; the automatic source must be an Eufy SDK Last event image.
To keep managing captures through a separate automation instead, explicitly set
`auto_capture: false` before enabling that automation.

The integration freezes each live Person, Stranger, or Package detection's
label and timestamp, requests the SDK's supported Last event refresh, and
compares image bytes against the baseline observed for that event. It writes
those same checked bytes, rather than taking a second snapshot that could have
advanced to another event. The filename uses the detection time in HA's
configured timezone. The SDK image state is a download time and can reset when
cached pictures are loaded after restart; it is not used as the event time.

A changed image can arrive more than five minutes later while the same
detection is still current. The pending event and baseline hash are persisted
under HA's `.storage` folder, so a restart does not discard the association or
turn an unchanged cached image into a new event. Subsequent detections supersede
pending metadata, including types outside Person/Package. A five-second
allowance handles an image state callback arriving just before its detection.
Only hashes and event metadata are journaled; image history stays in the
configured snapshot directory. Refresh requests are coalesced while one is
already in flight and do not start live video.

Dashboard 1.0.11 shows when capture is waiting for changed image bytes, when a
source is unavailable, or when writing fails. The same information is returned
as `source.capture` by `front_door_timeline.diagnostics`. A detection with no
changed picture does not create a duplicate or a hidden `image_update` file.

Detections from before tracking was installed cannot be reconstructed from
cached image timestamps. Old unclassified snapshots are not automatically
relabelled. The bridge still has to supply a usable event picture; if its bytes
remain unchanged, the card reports that condition while retaining saved history.

### Empty timeline and retained history in 1.0.11

Use backend **1.0.13** and `ha-custom-dashboards` **1.0.12** for current behavior.
Restart HA and fully reload the dashboard/Fully Kiosk page. Latest follows the
latest saved date for the selected tab. A day containing only `image_update` or
`motion` files no longer hides an older day containing Person or Package images.
Switching tabs while following Latest selects that tab's latest date. Choosing
a date explicitly keeps that date selected, including when it has no matches.
Tab counts still show the selected date's snapshots.

The index accepts optional `category=person` or `category=package`. Its default
without a category is unchanged. Images and both tab counts are available for
the selected day; additional `latest_dates`, per-date category counts,
`hidden_count`, and `label_counts` describe retained history.

If the card is empty, run this in **Developer Tools → Actions** and inspect the
action response:

```yaml
action: front_door_timeline.diagnostics
data: {}
```

The response includes the configured storage directory, retention, file counts,
saved labels, latest dates, recent filenames, current source/detection states,
and registered Eufy Last event image entities. `hidden_count` counts saved files
whose labels belong to neither visible tab. A missing/unreadable folder is
reported as a storage error. No snapshots are captured, refreshed, relabeled,
or deleted. An optional `entity_id` inspects another source image explicitly.

The backend pairs Eufy source images and detections by registry unique IDs,
including entity names with a `_2` suffix. This does not change an installed
automation's watched entity: the diagnostics list helps identify when an
automation still watches an unavailable entity after a reinstall. Reinstalling
can also give a retained cached image a new SDK update timestamp; a timestamp
alone does not establish the event's original capture time or classification.

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

In legacy mode (`auto_capture: false`), the existing automation can stay in place.
The legacy example uses the 300-second bound. In 1.0.11 the backend resolves renamed SDK
source/detection pairs through the entity registry, falling back to the
documented naming pair for other image sources.
Historical `image_update` snapshots cannot be classified reliably without their
original detection metadata and are not automatically relabeled.

Update this integration to **1.0.12** and `ha-custom-dashboards` to **1.0.11**, restart
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
The manual action's filename uses the **source image update timestamp**. This
timestamp can represent a cache reload rather than the event itself. Identical
content is still suppressed. Automatic capture uses its tracked detection time.
After the button returns, the action waits up to 20 seconds for a background
image update if the existing image is already archived or unavailable. A saved
image response contains its filename, label, and source timestamp. If nothing
new arrives, the action raises a clear error containing the source timestamp and
the existing filename instead of returning a silent success.

If no Person/Package detection can be matched to the source image, the action
adds no hidden `image_update` entry and reports the classification problem. After
visually checking the source image, you can explicitly provide `label: person`
or `label: package` to sync that verified category.

Install backend **v1.0.12**, use dashboard **v1.0.11 or later**, and restart HA.
Regular automatic capture now runs inside the integration; turn off the old automation.
This action does not start a live stream. It cannot force a HomeBase to return a cover it has not
provided. The bridge's [current HomeBase 3 report](https://github.com/mega-yfue/ha-eufy-sdk-bridge/issues/134)
and [troubleshooting guide](https://github.com/mega-yfue/ha-eufy-sdk-bridge/blob/main/docs/troubleshooting.md)
describe cached thumbnails, missing local covers, and push/DNS failures. If the
source stays stale, inspect bridge log entries containing `/event-image`,
`local refresh`, `force refresh`, or `cover reply`; a `200 cached thumbnail` is
not confirmation that a fresh frame arrived.

The legacy automation example is for `auto_capture: false` only. It watches image
timestamps and stores current detection types from `event.front_door_detection`.
YAML examples are not automatically applied by HACS. Unknown or stale metadata
uses `image_update`, hidden by the Person/Package tabs. Automatic capture in
1.0.12 replaces this mechanism with detection tracking and changed-image checks.

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
deleted or relabeled by the update. Duplicate protection is retained in 1.0.12.

To inspect skipped captures, temporarily enable info logging for
`custom_components.front_door_timeline`. A skipped capture reports
`Skipped duplicate snapshot ... image bytes are unchanged from ...`. This means
the image source supplied the same data; deduplication cannot make Eufy deliver
a fresh frame. Byte-identical repeats are suppressed; this is not a visual or
perceptual comparison, so recompressed images can still be distinct.

## Icons

The repository contains a custom icon in 256px and 512px PNG, with editable SVG source. For integrations, `brand/icon.png` and `brand/icon@2x.png` are bundled inside the integration; Home Assistant 2026.3+ can display these local brand images.

HACS currently uses its external brands source for integration list icons; the open upstream issue https://github.com/hacs/integration/issues/5223 prevents local-only brand assets from reliably appearing there. Dashboard repositories use HACS's generic Dashboard category icon rather than a per-repository custom icon. The artwork is visible in each repository README and is included for supported HA surfaces. No unsupported icon field is added to hacs.json.

