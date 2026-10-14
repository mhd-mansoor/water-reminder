"""Automated tests for Water Reminder. Run with:  python -m pytest -q tests"""
import datetime
import http.server
import json
import os
import threading

import pytest


def pump(app, ms=50):
    """Run the Qt event loop for a moment (and delete closed windows)."""
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


# --------------------------------------------------------------------------- config & state
def test_defaults_and_config_roundtrip(wr):
    cfg = wr.load_config()
    assert cfg["interval_minutes"] == 30 and cfg["daily_goal_glasses"] == 8
    cfg["interval_minutes"] = 45
    wr.save_config(cfg)
    assert wr.load_config()["interval_minutes"] == 45


def test_broken_config_falls_back_to_defaults(wr):
    os.makedirs(wr.CONFIG_DIR, exist_ok=True)
    with open(wr.CONFIG_FILE, "w") as f:
        f.write("{not json")
    assert wr.load_config()["interval_minutes"] == 30


def test_record_and_undo_glass(wr):
    assert wr.load_state()["count"] == 0
    wr.record_glass()
    st = wr.record_glass()
    assert st["count"] == 2 and st["total"] == 2 and len(st["times"][st["date"]]) == 2
    st = wr.undo_glass()
    assert st["count"] == 1 and st["total"] == 1
    wr.undo_glass()
    assert wr.undo_glass()["count"] == 0  # never below zero


def test_streaks_and_achievements(wr):
    st = wr.load_state()
    today = datetime.date.today()
    for i, c in enumerate([8, 9, 8, 3, 8, 8]):  # yesterday, 2 days ago, ...
        st["history"][(today - datetime.timedelta(days=i + 1)).isoformat()] = c
    st["count"] = 8
    wr.save_state(st)
    s = wr.compute_stats(goal=8)
    assert s["streak"] == 4          # today + 3 previous days
    assert s["best"] == 4
    assert s["overachiever"] is False
    st["history"][(today - datetime.timedelta(days=10)).isoformat()] = 11
    wr.save_state(st)
    assert wr.compute_stats(goal=8)["overachiever"] is True


def test_export_csv(wr, tmp_path):
    wr.record_glass()
    out = tmp_path / "h.csv"
    assert wr.export_csv(str(out)) == 1
    rows = out.read_text().strip().splitlines()
    assert rows[0].startswith("date,glasses,ml") and ",1,250," in rows[1]


# --------------------------------------------------------------------------- scheduling rules
def test_quiet_hours_wraps_midnight(wr, monkeypatch):
    cfg = dict(wr.DEFAULTS)

    class FakeDT(datetime.datetime):
        current = datetime.datetime(2026, 1, 1, 23, 30)

        @classmethod
        def now(cls, tz=None):
            return cls.current
    monkeypatch.setattr(wr.datetime, "datetime", FakeDT)
    assert wr.in_quiet_hours(cfg)
    FakeDT.current = datetime.datetime(2026, 1, 1, 7, 59)
    assert wr.in_quiet_hours(cfg)
    FakeDT.current = datetime.datetime(2026, 1, 1, 12, 0)
    assert not wr.in_quiet_hours(cfg)


def test_smart_pacing(wr):
    cfg = dict(wr.DEFAULTS)
    noon = datetime.datetime.combine(datetime.date.today(), datetime.time(15, 0))
    assert wr.next_interval(cfg, count=8, now=noon) == 45          # goal met: relax
    assert wr.next_interval(cfg, count=1, now=noon) == 21          # behind: 30 * 0.7
    assert wr.next_interval(cfg, count=4, now=noon) == 30          # on track
    cfg["smart_pacing"] = False
    assert wr.next_interval(cfg, count=8, now=noon) == 30


def test_version_compare(wr):
    v = wr._version_tuple
    assert v("v1.10.0") > v("1.9.9") > v("1.4") and v("1.4.0") == v("1.4")


def test_checksum_verification(wr, tmp_path):
    f = tmp_path / "WaterReminder-Windows.zip"
    f.write_bytes(b"hello")
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(f"{wr._sha256(str(f))}  WaterReminder-Windows.zip\n")
    assert wr.verify_download(str(sums), f.name, str(f))
    f.write_bytes(b"tampered")
    assert not wr.verify_download(str(sums), f.name, str(f))
    assert not wr.verify_download(str(sums), "missing.zip", str(f))


def test_update_asset_names(wr, monkeypatch):
    monkeypatch.setattr(wr, "IS_FROZEN", False)
    assert wr.update_asset_name() == "water_reminder.py"
    monkeypatch.setattr(wr, "IS_FROZEN", True)
    name = wr.update_asset_name()
    assert name in ("WaterReminder-Windows.zip", "WaterReminder-macOS.dmg") or name.startswith("WaterReminder-Linux-")


# --------------------------------------------------------------------------- animation
@pytest.mark.parametrize("reduce_motion", [False, True])
def test_scene_full_lifecycle_and_drawing(wr, app, reduce_motion):
    from PySide6.QtGui import QImage, QPainter
    cfg = dict(wr.DEFAULTS, reduce_motion=reduce_motion)
    s = wr.Scene(1600, 900, cfg, 3, "Test")
    img = QImage(1600, 900, QImage.Format_ARGB32_Premultiplied)

    def draw():
        img.fill(0)
        p = QPainter(img)
        s.draw_fx(p)
        ox, oy = s.origin()
        s.draw_mascot(p, (ox, oy, s.win_w, s.win_h))
        p.end()
    for _ in range(150):           # enter + some roaming
        s.update(1 / 30)
    assert s.state == "idle"
    draw()
    assert "drank" in s.buttons and "snooze" in s.buttons and "settings" in s.buttons
    s.leave("drank")
    assert s.count == 4
    for _ in range(400):
        s.update(1 / 30)
        if _ % 40 == 0:
            draw()
        if s.done:
            break
    assert s.done and s.result == "drank"


def test_empty_glasses_are_visible(wr, app):
    """Regression: unfilled glasses in the speech bubble must be drawn (not invisible)."""
    from PySide6.QtGui import QImage, QPainter
    s = wr.Scene(1600, 900, dict(wr.DEFAULTS), 0, "Test")
    for _ in range(150):
        s.update(1 / 30)
    img = QImage(1600, 900, QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    s.draw_mascot(p)
    p.end()
    bx, by, bw, bh = s.bubble_rect
    # sample the left of the progress row: find bluish outline pixels of empty glasses
    y0, y1 = int(by + bh * 0.45), int(by + bh * 0.7)
    blue = 0
    for y in range(y0, y1):
        for x in range(int(bx + 15), int(bx + 120)):
            c = img.pixelColor(x, y)
            if c.alpha() > 200 and c.blue() > 180 and c.red() < 170:
                blue += 1
    assert blue > 30, f"empty glasses look invisible ({blue} outline pixels)"


# --------------------------------------------------------------------------- service behaviour
class _FakeOverlay:
    shown = []

    def __init__(self, cfg, count, on_done, on_settings=None, intro=None):
        _FakeOverlay.shown.append(intro)
        self.on_done = on_done


@pytest.fixture
def service(wr, app, monkeypatch):
    _FakeOverlay.shown = []
    monkeypatch.setattr(wr, "Overlay", _FakeOverlay)
    monkeypatch.setattr(wr, "in_quiet_hours", lambda cfg: False)
    monkeypatch.setattr(wr, "play_sound", lambda kind: None)
    state = {"idle": 0, "busy": None}
    monkeypatch.setattr(wr.Probes, "idle_seconds", classmethod(lambda c: state["idle"]))
    monkeypatch.setattr(wr.Probes, "busy_reason", classmethod(lambda c: state["busy"]))
    svc = wr.Service(app)
    svc.schedule(30)
    svc.fake = state
    yield svc
    svc.beat.stop()
    svc.precise.stop()


def _due_now(svc):
    svc.due_at = datetime.datetime.now() - datetime.timedelta(seconds=1)


def test_waits_while_busy_then_shows(wr, service):
    service.fake["busy"] = "a call"
    _due_now(service)
    service._heartbeat()
    assert _FakeOverlay.shown == [] and service.waiting_reason == "a call"
    service.fake["busy"] = None
    service._heartbeat()
    assert _FakeOverlay.shown == [None]
    service.overlay.on_done("drank")
    assert wr.load_state()["count"] == 1 and service.due_at is not None


def test_pauses_when_away_and_welcomes_back(wr, service):
    service.fake["idle"] = 600
    service._heartbeat()
    assert service.away and service.status_text().startswith("Paused")
    service.away_since = datetime.datetime.now() - datetime.timedelta(minutes=40)
    service.fake["idle"] = 1
    service._heartbeat()
    assert not service.away and service.intro == "welcome"


def test_short_break_resumes_countdown(wr, service):
    service.fake["idle"] = 400
    service._heartbeat()
    remaining = service.frozen_remaining
    service.fake["idle"] = 2
    service._heartbeat()
    left = (service.due_at - datetime.datetime.now()).total_seconds()
    assert abs(left - remaining) < 5 and service.intro is None


def test_settings_and_stats_reopen_after_close(wr, app, service):
    """Regression: on Windows, Settings could not be opened a second time."""
    for opener, attr in ((service.open_settings, "settings_dlg"), (service.open_stats, "stats_dlg")):
        for _ in range(3):
            opener()
            dlg = getattr(service, attr)
            assert dlg is not None and dlg.isVisible()
            dlg.reject()
            pump(app)
            assert getattr(service, attr) is None


def test_settings_save_applies(wr, app, service):
    service.open_settings()
    dlg = service.settings_dlg
    dlg.interval.setValue(45)
    dlg.motion.setChecked(True)
    dlg._save()
    cfg = wr.load_config()
    assert cfg["interval_minutes"] == 45 and cfg["reduce_motion"] is True
    left = (service.due_at - datetime.datetime.now()).total_seconds() / 60
    expected = wr.next_interval(cfg)  # 45, or a bit less if behind on the goal (smart pacing)
    assert expected - 1 < left <= expected + 0.1


# --------------------------------------------------------------------------- updater (local server)
@pytest.fixture
def release_server(tmp_path):
    payload = {"tag_name": "v99.0.0", "body": "## New\n- things", "html_url": "http://x",
               "assets": [{"name": "SHA256SUMS.txt", "browser_download_url": "http://x/s"}]}
    body = json.dumps(payload).encode()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/latest"
    srv.shutdown()


def test_update_check_finds_newer_version(wr, app, release_server):
    up = wr.Updater()
    up.API = release_server
    got = {}
    up.check(lambda info, err: got.update(info=info, err=err))
    for _ in range(100):
        pump(app, 20)
        if got:
            break
    assert got.get("err") is None and got["info"]["version"] == "99.0.0"
    dlg = wr.UpdateDialog(up, got["info"], app)  # the dialog builds without errors
    dlg.close()
