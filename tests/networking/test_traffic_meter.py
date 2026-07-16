from blrec.networking.traffic import TrafficMeter


def test_meter_reports_application_bytes_and_one_second_rates() -> None:
    now = [100.0]
    meter = TrafficMeter(clock=lambda: now[0])
    meter.record('eth0', 'upload', 'up', 1024)
    meter.record('eth0', 'upload', 'down', 128)

    first = meter.snapshot()[0]
    assert first.upload_total == 1024
    assert first.download_total == 128
    assert first.upload_bps == 0
    assert first.download_bps == 0

    now[0] = 101.0
    meter.record('eth0', 'upload', 'up', 2048)
    meter.record('eth0', 'upload', 'down', 256)
    second = meter.snapshot()[0]

    assert second.upload_total == 3072
    assert second.download_total == 384
    assert second.upload_bps == 2048
    assert second.download_bps == 256


def test_meter_keeps_purposes_separate_on_one_interface() -> None:
    meter = TrafficMeter()
    meter.record('eth0', 'recording', 'down', 4096)
    meter.record('eth0', 'danmaku', 'down', 32)

    snapshots = {(item.interface_name, item.purpose): item for item in meter.snapshot()}

    assert snapshots[('eth0', 'recording')].download_total == 4096
    assert snapshots[('eth0', 'danmaku')].download_total == 32
