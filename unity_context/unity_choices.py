"""Small authorized pick lists for visual action forms."""


def named_items(items, key="id", titles=("name", "title", "app_name")):
    if not isinstance(items, list):
        return []
    result = []
    for item in items[:200]:
        if not isinstance(item, dict) or not isinstance(item.get(key), str):
            continue
        title = next((item[name] for name in titles if isinstance(item.get(name), str) and item[name]), item[key])
        result.append((item[key], title[:160]))
    return result


def dictionary(value):
    return value if isinstance(value, dict) else {}


def device_choices(state):
    phone = state.get("media", {})
    desktop = state.get("desktop_media", {})
    sound = state.get("sound", {})
    desktop_sound = state.get("desktop_sound", {})
    calls = state.get("call_control", {})
    call_audio = state.get("call_audio", {})
    screen = state.get("screen", {})
    notifications = state.get("notifications", {})
    notices = list(notifications.values()) if isinstance(notifications, dict) else []
    result = {
        "media-session": named_items(phone.get("players"), "session", ("app_name", "title")),
        "desktop_media-session": named_items(desktop.get("players"), "session", ("app_name", "title")),
        "phone-output": named_items(dictionary(sound.get("media_output")).get("routes")),
        "call": named_items(calls.get("calls"), titles=("state",)),
        "call-output": named_items(call_audio.get("outputs")),
        "microphone": named_items(call_audio.get("microphones")),
        "notification": named_items(notices, titles=("app_name", "app")),
        "photo": named_items(state.get("photos", {}).get("items")),
        "remote-file": named_items(state.get("browsing", {}).get("items")),
        "peripheral": named_items(state.get("peripherals", {}).get("devices")),
    }
    result["notification-action"] = [choice for notice in notices if isinstance(notice, dict)
                                     for choice in named_items(notice.get("actions"), titles=("title",))]
    result["track"] = named_items(desktop.get("players"), "track_id", ("title",))
    if isinstance(desktop_sound.get("output"), str):
        result["desktop-output"] = [(desktop_sound["output"], desktop_sound.get("name", "Current laptop output"))]
    if isinstance(screen.get("session_id"), str):
        result["screen-session"] = [(screen["session_id"], "Current screen share")]
    return result
