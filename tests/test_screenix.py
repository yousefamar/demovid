from demovid.screenix import iter_events, recording_stamp


def test_recording_stamp_maps_screenix_names_to_demovid_dirs():
    assert recording_stamp("recording_20260903_162307") == "2026-09-03-16-23-07"
    assert recording_stamp("weird") == "weird"


def test_events_are_denormalised_and_typed():
    meta = {
        "click_events": [
            {"timestamp": 10.3, "x": 0.17864583, "y": 0.575, "button": 1, "event_type": "down"},
            {"timestamp": 10.4, "x": 0.17864583, "y": 0.575, "button": 3, "event_type": "up"},
        ],
        "key_events": [
            {"timestamp": 2.31, "keycode": 56, "label": "Alt", "is_modifier": True, "pressed": True},
            {"timestamp": 2.63, "keycode": 2, "label": "1", "is_modifier": False, "pressed": True},
            {"timestamp": 2.75, "keycode": 2, "label": "1", "is_modifier": False, "pressed": False},
            {"timestamp": 2.88, "keycode": 56, "label": "Alt", "is_modifier": True, "pressed": False},
        ],
    }
    cursor = [{"timestamp": 0.006, "x": 673, "y": 599}]
    events = sorted(iter_events(meta, cursor, 1920, 1080), key=lambda e: e["t"])
    assert events[0] == {"t": 0.006, "kind": "cursor", "x": 673, "y": 599}
    keys = [e for e in events if e["kind"] == "key"]
    assert keys[0] == {"t": 2.31, "kind": "key", "key": "alt", "state": "down", "mods": []}
    assert keys[1] == {"t": 2.63, "kind": "key", "key": "1", "state": "down", "mods": ["alt"]}
    assert keys[3]["mods"] == []  # releasing alt reports the mods held besides itself
    buttons = [e for e in events if e["kind"] == "button"]
    assert buttons[0] == {"t": 10.3, "kind": "button", "button": "left", "state": "down", "x": 343, "y": 621}
    assert buttons[1]["button"] == "right" and buttons[1]["state"] == "up"
