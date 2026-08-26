# Hardware

Everything you need to buy, wire and tune before installing SecureCam.

## Bill of materials

| Part                                                 | Why this one                                                                                        | Approx. cost |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------------------- | ------------ |
| Raspberry Pi 4 Model B, 4 GB                         | Hardware H.264 encoder, enough RAM for MediaMTX plus the control plane. 2 GB works; 8 GB is wasted. | $55          |
| Official Raspberry Pi Camera Module 3 (or v2, or HQ) | Connects to the CSI ribbon port and is driven natively by `libcamera`.                              | $25-50       |
| HC-SR501 PIR motion sensor                           | Cheap, 3.3 V-safe output, adjustable sensitivity and hold time.                                     | $2           |
| 3x female-to-female jumper wires                     | Straight onto the 40-pin header, no soldering.                                                      | $1           |
| microSD card, 32 GB+, A1/A2 rated                    | Continuous writing kills cheap cards. A2 endurance cards are worth it.                              | $10-20       |
| Official 5 V 3 A USB-C power supply                  | Undervoltage causes camera dropouts that look like software bugs.                                   | $8           |
| Case with a camera cutout                            | Passive cooling is enough for this workload. **No fan is needed.**                                  | $10          |

Optional: a NoIR camera module plus an IR illuminator if you need night vision. See [Night vision](#night-vision).

## Camera

The camera connects to the **CSI** port (the one nearest the audio jack on a Pi 4), not the DSI display port.

1. Power the Pi off completely.
2. Lift the plastic latch on the CSI connector.
3. Insert the ribbon with the **silver contacts facing the HDMI ports**.
4. Push the latch back down. The ribbon should not move if you tug it gently.

Verify after boot:

```bash
rpicam-hello --list-cameras
```

You should see one camera with its resolution modes. If the list is empty, see [troubleshooting.md](troubleshooting.md).

> A Camera Module 3 needs the `imx708` driver, present in Bookworm and later. On older images the camera will simply not appear.

## PIR sensor

### Wiring

The HC-SR501 has three pins. On most boards they are labelled underneath.

| PIR pin | Pi physical pin                  | Pi signal       |
| ------- | -------------------------------- | --------------- |
| VCC     | 2 (or 4)                         | 5 V             |
| OUT     | 11                               | GPIO17 (BCM 17) |
| GND     | 6 (or 9, 14, 20, 25, 30, 34, 39) | Ground          |

```
   Pi 40-pin header (looking at the board, USB ports at the bottom)

   3V3  (1) (2)  5V      <-- PIR VCC
 GPIO2  (3) (4)  5V
 GPIO3  (5) (6)  GND     <-- PIR GND
 GPIO4  (7) (8)  GPIO14
   GND  (9) (10) GPIO15
GPIO17 (11) (12) GPIO18  <-- PIR OUT to pin 11
```

The HC-SR501 has an on-board regulator, so it wants **5 V on VCC**, but its OUT pin still swings to 3.3 V. That is why it is safe to wire directly to a GPIO input. **Confirm this for any other PIR module before you connect it** — a 5 V output will damage the Pi.

If GPIO17 is already used on your board, pick any free BCM pin and set it during installation or afterwards:

```yaml
motion:
  gpio: 27
```

### Jumpers and potentiometers

The HC-SR501 has two orange potentiometers and one jumper.

| Control                 | Set it to                                   | Why                                                                                                                                                          |
| ----------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Sensitivity** pot     | Start at the middle                         | Fully clockwise gives ~7 m range and a lot of false triggers from heat sources.                                                                              |
| **Time delay** pot      | Fully **counter-clockwise** (minimum, ~3 s) | SecureCam does its own event timing with `motion.post_motion_seconds`. A long hardware delay just makes the sensor lie about when motion stopped.            |
| **Trigger mode** jumper | **H** (repeat trigger)                      | In H mode the output stays high while motion continues, which is what SecureCam's state machine expects. In L mode you get one pulse and then a dead period. |

### Placement

- Mount it **2-2.5 m high**, angled slightly down.
- Point it **across** the area people walk through, not straight at it. PIR sensors detect movement between zones, so someone walking towards the sensor triggers it much later than someone walking past.
- Keep it away from: direct sunlight, heating vents, radiators, windows that get sun, anything that moves and is warm.
- The plastic lens must not be behind glass. Glass blocks the infrared the sensor works with, so a PIR pointed out of a window sees nothing at all.

### Testing before you install

```bash
sudo ./scripts/pir-test.py --gpio 17
```

It prints the raw pin state and every transition for 60 seconds. Wave at the sensor. If nothing changes:

- Check the trigger jumper is on **H**.
- Give it **60 seconds to warm up** after power-on. A cold HC-SR501 outputs nonsense.
- Swap the OUT wire to a different GPIO and re-run with `--gpio N`.
- Try `--pull up` or `--active-low` if your module has an open-collector output.

## Power

Video encoding plus continuous SD writes is a sustained load. Symptoms of an inadequate supply:

```bash
vcgencmd get_throttled
```

| Value           | Meaning                                                |
| --------------- | ------------------------------------------------------ |
| `throttled=0x0` | Healthy.                                               |
| bit 0 set       | Undervoltage **right now** — replace the power supply. |
| bit 16 set      | Undervoltage happened at some point since boot.        |
| bit 1 / bit 17  | Frequency capped due to temperature.                   |

Use the official supply or a known-good 5 V 3 A one. Phone chargers and unpowered USB hubs are the usual culprits.

## Cooling

At 1080p15 with hardware H.264, a Pi 4 in a ventilated case sits at 50-65 °C. **No fan is required**, which is deliberate — a fan in a security camera is a moving part that fails and a noise that gives the camera away.

Check it:

```bash
vcgencmd measure_temp
```

If you are above 75 °C consistently:

- Add a heatsink or a case with more venting.
- Drop `camera.fps` from 15 to 10.
- Drop the resolution to 1280x720.
- Move the Pi out of direct sun.

## Storage

The rolling buffer writes continuously. At the default 3 Mbit/s that is roughly **32 GB per day** of writes to the card. That is well within an A1/A2 card's endurance rating, but not within a cheap no-name card's.

If you want the card to last for years, put `/var/lib/securecam` on a USB SSD instead:

```bash
sudo systemctl stop securecam securecam-mediamtx
sudo mkfs.ext4 /dev/sda1
sudo mkdir -p /mnt/securecam
echo '/dev/sda1 /mnt/securecam ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
sudo mount -a
sudo rsync -a /var/lib/securecam/ /mnt/securecam/
sudo chown -R securecam:securecam /mnt/securecam
```

Then point the config at it:

```yaml
storage:
  base_path: /mnt/securecam
  events_path: /mnt/securecam/events
  buffer_path: /mnt/securecam/buffer
```

```bash
sudo -u securecam securecam-admin check-config
sudo systemctl start securecam-mediamtx securecam
```

## Night vision

The standard camera module has an infrared filter and sees nothing in the dark. For night coverage you need:

1. A **NoIR** camera module (the filter is omitted).
2. An **IR illuminator** — either the LED ring built into some third-party modules, or a separate 850 nm floodlight.

A NoIR module needs its own tuning file or colours will be badly wrong in daylight:

```yaml
camera:
  tuning_file: /usr/share/libcamera/ipa/rpi/vc4/imx219_noir.json
```

Use `imx708_noir.json` for a Camera Module 3 NoIR. List what is available:

```bash
ls /usr/share/libcamera/ipa/rpi/vc4/
```

> IR LEDs mounted **inside** the same enclosure as the lens will reflect off the housing and wash out the whole image. Mount the illuminator separately, or use a module designed with the LEDs shielded from the lens.

## Enclosures and outdoor use

None of this is weatherproof. For outdoor mounting you need an IP65 enclosure with a glass window for the camera — and the PIR sensor must be **outside** the glass, since glass blocks infrared. Most people mount the PIR through a separate hole with its own lens dome exposed.

Condensation is the real enemy outdoors. Add a silica gel pack, and mount the enclosure tilted slightly forward so water runs off the window.
