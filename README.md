# ASUSTOR AS6510T TrueNAS front-panel app

This is a TrueNAS Custom App for the ASUSTOR AS6510T front panel.

It provides:

- A steady status LED after the controller reaches readiness.
- A two-line LCD status display.
- LCD power timeout after 30 seconds.
- LCD wake on any LCD button event.
- SMART polling for configured disks.
- Red disk alert LEDs for SMART faults.

## Important security note

The container is privileged because the AS6510T front LEDs use legacy I/O ports and SMART requires direct access to host disk devices. This gives the container broad access to the TrueNAS host. Only build and run this image from source that you trust.

## Build

The TrueNAS Apps service must be configured first. Build the image on the NAS or in a compatible Linux Docker environment:

```sh
docker build -t asustor-frontpanel:0.1.6 .
```

The app YAML is `compose.yaml`. In TrueNAS, choose **Apps → Discover → Custom App → Install via YAML**, then paste that file. The YAML expects the image tag above to exist locally or in a registry accessible to the Apps service.

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

The status LED is set steady on when the service starts. It is not used as a disk fault indicator. A disk with a failed SMART health result, a nonzero ATA error log count, a nonzero error-information log count, an inaccessible configured path, or a SMART command failure gets a red LED. Healthy disks have their red alert LED turned off; the existing green/activity state is left alone.

SMART checks run every five minutes and use `smartctl -d sat -n standby,0`, so sleeping disks are not spun up solely by this app. A disk left in standby is treated as unknown/healthy until a later active check.

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
