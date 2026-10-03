# Home Assistant Front Door Snapshot Timeline

This package saves timestamped images from `camera.front_door` and displays them in a Home Assistant card with:

- a date picker at the top;
- previous, next, latest, and refresh controls;
- a large, uncropped selected image with its date, time, and event label;
- a horizontally scrollable, touch-friendly thumbnail timeline;
- automatic refresh and full-screen image viewing;
- authenticated image access (snapshots are not exposed through `/local`);
- automatic 30-day retention, configurable in YAML.

The automation uses the likely Eufy motion entity `binary_sensor.front_door_motion_detected`. Change that one entity ID if your detector is named differently.

## Files to copy

Copy the package contents into the matching Home Assistant paths:

| Package path | Home Assistant path |
| --- | --- |
| `custom_components/front_door_timeline/` | `/config/custom_components/front_door_timeline/` |
| `www/front-door-timeline-card.js` | `/config/www/front-door-timeline-card.js` |

Keep every file in the custom component folder together: `__init__.py`, `const.py`, `manifest.json`, and `services.yaml`.

## 1. Add the integration configuration

Add this top-level block to `/config/configuration.yaml`:

```yaml
front_door_timeline:
  directory: /media/front_door_snapshots
  retention_days: 30
```

Do not put the snapshots in `/config/www`. The custom integration serves `/media/front_door_snapshots` only through authenticated Home Assistant API requests.

Run **Settings > System > Repairs > three dots > Check configuration**, then fully restart Home Assistant.

## 2. Test one snapshot

Open **Settings > Developer tools > Actions**, switch to YAML mode, and run:

```yaml
action: front_door_timeline.capture
data:
  entity_id: camera.front_door
  label: manual
```

The action should complete without an error. The integration creates `/media/front_door_snapshots` automatically.

### If the saved Eufy image is old

Many Eufy devices provide an event-image entity. If you have `image.front_door_event_image`, use it in the capture action instead:

```yaml
action: front_door_timeline.capture
data:
  entity_id: image.front_door_event_image
  label: motion
```

That is usually preferable for a battery doorbell because it saves Eufy's event frame without starting a new live stream. Keep the 3-second delay in the automation so the new event image has time to arrive.

If you have no event-image entity and `camera.front_door` remains stale, see **Eufy fresh-frame fallback** below.

## 3. Add the snapshot automation

Open **Settings > Automations & scenes > Create automation > Create new automation**, open the three-dot menu, choose **Edit in YAML**, and paste `examples/automation.yaml`:

```yaml
alias: Front Door - Save snapshots to timeline
description: Save one Eufy front-door image per motion event for the private dashboard timeline.
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.front_door_motion_detected
    to: "on"
    id: motion
conditions: []
actions:
  - delay:
      hours: 0
      minutes: 0
      seconds: 3
      milliseconds: 0
  - action: front_door_timeline.capture
    data:
      entity_id: camera.front_door
      label: >-
        {{ trigger.id if trigger is defined and trigger.id else 'manual' }}
mode: queued
max: 10
```

Save it. It is compatible with Home Assistant's visual editor after pasting.

Use only one Eufy detector as the basic trigger. Adding motion and person triggers often produces two images for the same event. If you prefer person-only captures, replace the trigger entity with your Eufy person-detected binary sensor.

## 4. Register the dashboard card

Open **Settings > Dashboards > three-dot menu > Resources > Add resource** and enter:

- URL: `/local/front-door-timeline-card.js?v=1.0.1`
- Resource type: **JavaScript module**

If **Resources** is hidden, enable **Advanced mode** in your Home Assistant user profile first.

Reload the browser after adding the resource. If the card is still not found, clear the Home Assistant frontend cache or increment the URL suffix.

## 5. Add the card to a dashboard

Edit the dashboard, add a **Manual** card, and paste:

```yaml
type: custom:front-door-timeline
title: Front Door
refresh_seconds: 30
thumbnail_width: 170
show_event_labels: true
```

The most recent saved date opens automatically. The timeline starts at the newest image, and the arrow buttons skip between dates that actually contain snapshots. Click the large image for full-screen viewing.

## Eufy fresh-frame fallback

Use this automation only if both `camera.front_door` and the Eufy event-image entity are stale. It starts the P2P stream, waits for it, asks Eufy Security to generate a frame, saves it, and stops the stream. This uses more doorbell battery than saving the event image.

```yaml
alias: Front Door - Save fresh streamed snapshot
description: Start Eufy briefly and save a fresh frame to the private timeline.
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.front_door_motion_detected
    to: "on"
    id: motion
conditions: []
actions:
  - action: camera.turn_on
    target:
      entity_id: camera.front_door
  - wait_template: "{{ is_state('camera.front_door', 'streaming') }}"
    timeout:
      hours: 0
      minutes: 0
      seconds: 20
    continue_on_timeout: true
  - action: eufy_security.generate_image
    target:
      entity_id: camera.front_door
  - delay:
      hours: 0
      minutes: 0
      seconds: 1
      milliseconds: 0
  - action: front_door_timeline.capture
    data:
      entity_id: camera.front_door
      label: motion
  - action: camera.turn_off
    continue_on_error: true
    target:
      entity_id: camera.front_door
mode: queued
max: 5
```

Do not keep both the basic automation and this fallback enabled, or each event will be saved twice.

## Retention and privacy

- Snapshots older than `retention_days` are deleted at startup and daily at 3:17 AM.
- The cleanup routine deletes only files that match this package's `front_door_...jpg/png` naming pattern.
- Both the image list and image-file endpoints require Home Assistant authentication.
- The frontend card loads protected image bytes with the signed-in Home Assistant session and creates temporary in-browser image URLs.

To keep 90 days instead, change `retention_days: 30` to `retention_days: 90` and restart Home Assistant.
