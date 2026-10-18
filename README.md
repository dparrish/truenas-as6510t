# ASUSTOR AS6510T TrueNAS front-panel app

This is a TrueNAS Custom App for the ASUSTOR AS6510T front panel.

It provides:

- The hardware blink engine for the status LED is disabled after the controller reaches readiness.
- A two-line LCD status display.
- LCD power timeout after 30 seconds.
- LCD wake on any LCD button event.
- TrueNAS API-backed disk inventory and alert monitoring.
- Red disk alert LEDs for TrueNAS-reported disk faults.

## Important security note

The container is privileged because the AS6510T front LEDs use legacy I/O ports. It connects to the TrueNAS WebSocket API for disk and alert state, so it no longer runs `smartctl` itself. This still gives the container broad access to the host hardware; only build and run this image from source that you trust.

## Build

The TrueNAS Apps service must be configured first. Build the image on the NAS or in a compatible Linux Docker environment:

```sh
docker build -t asustor-frontpanel:0.1.11 .
```
The app YAML is `compose.yaml`. Set `TRUENAS_API_USERNAME` and `TRUENAS_API_KEY` to a TrueNAS API key before installing. The key must be able to read `disk.query` and `alert.list`. In TrueNAS 27, the app uses the JSON-RPC WebSocket endpoint `/api/current`.

## Disk-to-bay mapping

The default mapping is:

```text
1=/host-dev/sda
2=/host-dev/sdb
3=/host-dev/sdc
4=/host-dev/sdd
5=/host-dev/sde
```

This matches the current AS6510T enumeration observed during development. Linux device names are not a permanent physical-slot contract. Before relying on per-bay alerts, verify the mapping by serial number and one controlled disk identification test, then update `BAY_DEVICE_MAP` in the custom-app YAML. This app only changes the red alert GPIOs; it leaves the green/activity LEDs alone.

## Behavior

The status LED is disabled when the service starts. The app first removes the AS6510T status GPIO (IT8625E GP47) from the hardware blink engine, then drives it to the vendor-compatible off level. Disk inventory and health come from TrueNAS `disk.query` and `alert.list`; the app sets a bay's red LED when an active TrueNAS alert references that disk. Healthy disks have their red alert LED turned off; the existing green/activity state is left alone.

The LCD line 1 is `TrueNAS AS6510T`. Line 2 is one of:

```text
SMART OK n/n
SMART ERR D1,D2
SMART NO DISKS
```

The LCD is powered on at startup and after any valid LCD button event. It is powered off after 30 seconds without a button event. The serial protocol is the one implemented by https://github.com/mafredri/lcm.

## Rollback

Stop and delete the custom app. It only owns `/dev/ttyS1` and the documented IT87 GPIOs while running. If the app is stopped, the LCD is left powered off and disk LEDs remain at their last hardware state; restore them with the vendor-compatible driver or reboot the NAS.

## Tests

Run the pure logic tests on a development machine:

```sh
python3 -m unittest discover -s tests -v
```

Hardware tests must be performed on the AS6510T itself and should start with the LCD only. Do not write arbitrary values to `/dev/port` or Intel PCH GPIOs.
