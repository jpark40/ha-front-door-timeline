# Front Door Snapshot Timeline

![Repository icon](icon.png)

Version **1.0.14** saves the image available from `image.front_door_last_event`
whenever `event.front_door_detection` reports a relevant detection. The image's
timestamp and bytes do not have to change before a new detection is saved.

| Detection type | Timeline tab |
| --- | --- |
| `person` | Person |
| `stranger` | Person |
| `package_delivered` | Package |
| `package_taken` | Package |

Other detection types, including `package_stranded`, do not create new entries.
Existing history is preserved.

## Install or update

1. Add `https://github.com/jpark40/ha-front-door-timeline` under **HACS > Custom repositories**, type **Integration**, and download it.
2. Keep your existing `front_door_timeline:` configuration block. The defaults use `image.front_door_last_event` and its matching `event.front_door_detection`. For a new installation, merge [examples/configuration.yaml](examples/configuration.yaml) into `configuration.yaml` once.
3. Disable the old **Front Door - Save snapshots to timeline** automation. Automatic capture runs inside this integration by default.
4. Restart Home Assistant.
5. Update [HA Custom Dashboards](https://github.com/jpark40/ha-custom-dashboards) to **1.0.13 or later** and fully reload the browser/Fully Kiosk page.

No configuration edit is needed for an existing installation using the default
entity pair. You can explicitly set `source_entity` and `detection_entity` as in
the example. Without `detection_entity`, registry pairing supports renamed Eufy
entities before falling back to the documented naming pair.

HACS updates the existing integration directory. Snapshots stay in the configured
storage directory, normally `/media/front_door_snapshots`. Updating does not delete
or relabel existing snapshots. This integration uses YAML setup; it does not add a
UI configuration flow. The dashboard JavaScript comes from the separate dashboard
repository. Original manual installation notes are in `LEGACY-INSTALLATION.md`.

## Capture behavior

Automatic capture reads through Home Assistant's image API, the same source used
by the image entity on a dashboard. It does not open a camera stream or require a
bridge refresh button. Old `live_fallback` and `fallback_camera` configuration
keys remain accepted for compatibility but no longer enable video capture.

The detection timestamp and type identify one saved event. Repeated callbacks
for that event do not add copies. A different detection gets its own entry even
when Eufy is still displaying identical image bytes. The saved timestamp is the
detection time in Home Assistant's configured timezone.

If the image updates after detection, the integration atomically updates the same
snapshot while that detection remains current. It does not add another event.
Versioned image URLs and dashboard cache invalidation make the updated picture
visible. Image updates without a tracked relevant detection do not create entries.
A newer detection, including an ignored type, prevents a delayed image from being
saved with the previous detection's category. The image's own timestamp is
reported in diagnostics but does not gate capture.

An unavailable image or failed write is reported and retried on image recovery.
Event identity persists across restart, including migration of detections left
waiting by 1.0.12/1.0.13. Restored HA state alone does not create a new event.
The source image is the available Last event image; it may initially be the same
picture as the preceding event until Eufy updates it.

## Sync the currently displayed image

To save the currently available image with the current relevant detection, run:

```yaml
action: front_door_timeline.refresh_source
data:
  entity_id: image.front_door_last_event
```

In automatic mode, this directly syncs the configured image/detection pair even
when their timestamps are far apart. It returns `saved`, `updated`, or `unchanged`
and the file/event details. Repeating it does not add a second copy of that event.
It reports an error if the image is unavailable or the current detection is outside
the four supported types. The older bridge-refresh behavior is retained for
legacy manual mode and explicit verified category overrides.

## Diagnostics and history

```yaml
action: front_door_timeline.diagnostics
data: {}
```

This read-only action returns storage counts, labels, latest dates, source entity
states, and `source.capture`. Successful automatic captures report
`image_source: event_image`, `image_entity`, `event_timestamp`, `label`, and `file`.
An `image_unavailable` or `capture_error` status includes the actual failure.

The card opens the latest saved date for each tab and follows new dates and
snapshots unless you select history manually. Hidden older labels remain in
storage and are counted by diagnostics. Retention cleanup runs daily at 03:17 HA
local time and only removes recognized timeline files older than the configured
retention period. The API and images require Home Assistant authentication.

## Optional manual automation

Normal operation needs no automation. If you deliberately set
`auto_capture: false`, [examples/automation.yaml](examples/automation.yaml) watches
the detection event and saves the image with its event timestamp/type. This
optional mode captures the image once per detection; automatic mode also updates
the saved image if it arrives later. Do not run both capture mechanisms together.
The `capture` service still supports manual camera/image snapshots without an
`event_timestamp`; these retain the earlier content duplicate check.

## Releases and checks

Keep `VERSION` and the integration manifest version in agreement. Changes to
`VERSION` merged into `main` run validation and publish the matching GitHub/HACS
release automatically. The release workflow can also be run manually.

```sh
python3 scripts/validate.py
python3 -m unittest discover -s tests -v
```

One integration is included per HACS repository. Default-catalog submission is
separate from custom-repository installation.
