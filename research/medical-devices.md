# Reading personal medical equipment from A.R.S

Researched 2026-09-07 on the target machine (Apple Silicon, **macOS 26.6.2 build 25G83**,
Python 3.12, `bleak` 3.0.2, `pyobjc` 12.2.2).

**No radio was opened at any point.** No scan, no pairing, no connection, no device of any
kind was contacted. Every byte string below is hand-constructed from the Bluetooth SIG
specifications and decoded locally; every macOS claim is either read out of installed
framework headers / library source, or measured by calling a framework method that touches
no hardware. Where I could not measure, I say so and say what would settle it.

Two runnable artefacts back this document:

- `/Users/alexandrustratulat/ars/research/experiments/ble/sfloat_check.py` — 55 assertions
  over the IEEE-11073 float decode and all six measurement characteristics. **All pass.**
- `/Users/alexandrustratulat/ars/research/experiments/ble/device_backend_sketch.py` — the
  proposed seam, a BLE implementation sketch and a mock, producing real `VitalReading`
  objects from the real `ars_protocol` types. **Runs green** (11 readings from 5 packets,
  2 dropped by `PLAUSIBLE`).

---

## The decision

Alex wants A.R.S to read his own medical equipment, personal use first. Three things have
to be decided before any code is written:

1. **Do we support a *category* of device or a *product*?** That is entirely determined by
   whether the standard GATT profiles are actually implemented by things you can buy.
2. **Direct BLE, or Apple Health?** Many of these devices already write to HealthKit, and
   if HealthKit were readable that would be strictly less work and strictly more data.
3. **Which `VitalKind`s ship in increment 1**, and which are honestly out of reach.

## Recommendation in one paragraph

**Go direct to BLE. Apple Health is not an option on this machine — measured, not assumed:
`HKHealthStore.isHealthDataAvailable()` returns `False` on macOS 26.6.2.** Support the
*category* via the adopted Bluetooth SIG profiles, and refuse to reverse-engineer vendor
protocols in the product. **Ship two profiles in increment 1: Blood Pressure (0x1810) and
Heart Rate (0x180D).** They cover four of the ten `VitalKind`s — systolic, diastolic, heart
rate — with one cheap standards-compliant cuff (A&D UA-651BLE, ~€60) and one strap you may
already own (Polar H10). **Buy A&D, not Omron**: Omron's stored-record transfer is a
proprietary EEPROM protocol behind a 16-byte unlock key, and the community tool for it,
`omblepy`, still cannot read the newer encrypted models. **Do not build a weight-scale
backend at all yet** — the Weight Scale Service is designed for exactly one client and
explicitly never retransmits, so if Alex's phone app takes the reading first, A.R.S never
sees it, and almost nothing on the consumer market implements the standard anyway.
**Glucose is real but is the most work**: the measurement characteristic notifies nothing
until you drive a Record Access Control Point state machine, and the unit handling in the
SIG's own current documentation is internally contradictory (§1.8).

**The single highest-risk finding: three unit/sentinel bugs in this domain produce numbers
that `PLAUSIBLE` accepts.** A weight in pounds read as kilograms (176 kg), the weight
"measurement unsuccessful" sentinel `0xFFFF` (327.675 kg), and a mmol/L glucose read as
mg/dL (5.5) all sit inside the plausible range and would be stored silently as fact. The
range check in `health.py` is a good net; it does not catch these. §1.8 lists what does.

---

## 1. The standard profiles

### 1.1 Sources, with versions and dates

Everything in this section comes from Bluetooth SIG primary documents, downloaded and text-
extracted today:

| document | version / date | what it fixes |
|---|---|---|
| [Assigned Numbers](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Assigned_Numbers/out/en/Assigned_Numbers.pdf) | **2026-09-04** | every UUID below |
| [GATT Specification Supplement (GSS)](https://btprodspecificationrefs.blob.core.windows.net/gatt-specification-supplement/GATT_Specification_Supplement.pdf) | **2026-02-05** | byte layout of BP, temperature, glucose, weight, HR |
| [Blood Pressure Service (BLS) 1.1.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/BLS_v1.1.1/out/en/index-en.html) | 1.1.1 | properties, indicate-vs-notify |
| [Blood Pressure Profile (BLP) 1.1.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/BLP_v1.1.1/out/en/index-en.html) | 1.1.1 | security requirements |
| [Health Thermometer Service (HTS) 1.0](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HTS_v1.0/out/en/index-en.html) | 1.0 | properties |
| [Glucose Service (GLS) 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/GLS_v1.0.1/out/en/index-en.html) | 1.0.1 | **units, definitively** |
| [Glucose Profile (GLP) 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/GLP_v1.0.1/out/en/index-en.html) | 1.0.1 | security |
| [Weight Scale Service (WSS) 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/WSS_v1.0.1/out/en/index-en.html) | 1.0.1 | single-client rule, `0xFFFF` sentinel |
| [Body Composition Service (BCS) 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/BCS_v1.0.1/out/en/index-en.html) | 1.0.1 | MTU continuation packets |
| [Heart Rate Service (HRS) 1.0](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRS_v1.0/out/en/index-en.html) | 1.0 | properties |
| [Heart Rate Profile (HRP) 1.0](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRP_v1.0/out/en/index-en.html) | 1.0 | security, and its conditionality |
| [Pulse Oximeter Service (PLXS) 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/PLXS_v1.0.1/out/en/index-en.html) | 1.0.1 (2022-01-18) | PLX layout, SFLOAT special values |
| [Pulse Oximeter Profile (PLXP) 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/PLXP_v1.0.1/out/en/index-en.html) | 1.0.1 | security, mandatory bonding |

Terminology note, because it caused me two false starts: the current GSS renamed the
IEEE-11073 types. **`medfloat16` is what older specs call `SFLOAT`; `medfloat32` is
`FLOAT`.** GSS §2.1 says so explicitly. Everything below uses the old names because that is
what every existing implementation and every device datasheet uses.

### 1.2 The map: services, characteristics, and how readings arrive

All 16-bit UUIDs expand to `0000XXXX-0000-1000-8000-00805f9b34fb`. All multi-octet fields
are **little-endian** (each service spec restates this; e.g. PLXS §1.7).

| category | service | measurement characteristic | delivery | supporting characteristics |
|---|---|---|---|---|
| Blood pressure | Blood Pressure `0x1810` | Blood Pressure Measurement `0x2A35` | **Indicate** (mandatory) | Intermediate Cuff Pressure `0x2A36` (Notify, optional); BP Feature `0x2A49` (Read); Blood Pressure Record `0x2A52`+RACP (BLS 1.1.1 "Enhanced" only) |
| Pulse oximeter | Pulse Oximeter `0x1822` | PLX Spot-check `0x2A5E` | **Indicate** | PLX Continuous `0x2A5F` (**Notify**); PLX Features `0x2A60` (Read); RACP `0x2A52` (Indicate+Write) |
| Thermometer | Health Thermometer `0x1809` | Temperature Measurement `0x2A1C` | **Indicate** (mandatory) | Intermediate Temperature `0x2A1E` (Notify, optional); Temperature Type `0x2A1D`; Measurement Interval `0x2A21` |
| Glucometer | Glucose `0x1808` | Glucose Measurement `0x2A18` | **Notify** (mandatory) | Glucose Measurement Context `0x2A34` (Notify); Glucose Feature `0x2A51` (Read); **RACP `0x2A52` (Indicate+Write, mandatory)** |
| Weight scale | Weight Scale `0x181D` | Weight Measurement `0x2A9D` | **Indicate** (mandatory) | Weight Scale Feature `0x2A9E` (Read). Body composition is a *separate* service: Body Composition `0x181B` / Body Composition Measurement `0x2A9C` (Indicate) |
| Heart-rate strap | Heart Rate `0x180D` | Heart Rate Measurement `0x2A37` | **Notify** (mandatory) | Body Sensor Location `0x2A38` (Read); HR Control Point `0x2A39` (Write) |

Three consequences that bite immediately:

- **Four of the six are Indicate, not Notify.** Indications are acknowledged at the ATT
  layer; the device will not send the next one until the stack acks. This does not change
  the client API on macOS (§3), but it does mean a device can stall waiting for you.
- **You cannot `read` a measurement characteristic.** `0x2A35` has no Read property. A real
  person hit exactly this and filed
  [cordova-plugin-ble-central#27](https://github.com/don/cordova-plugin-ble-central/issues/27)
  trying `ble.read(addr, '1810', '2a35')` against an A&D UA-651BLE and getting nothing.
  You must write `0x0002` (indications) to the Client Characteristic Configuration
  descriptor and wait.
- **Glucose is not a subscribe-and-wait profile.** GLS §3.1.1: "When the Client
  Characteristic Configuration descriptor is configured for notifications the Record Access
  Control Point shall be used to control notifications of this characteristic." Subscribing
  alone yields nothing forever. You must write an op code to `0x2A52` (e.g. Report Stored
  Records / Last Record) and then consume the notification burst, terminated by an
  indication on the RACP itself.

### 1.3 IEEE-11073 `medfloat16` (SFLOAT) and `medfloat32` (FLOAT)

This is where implementations go wrong, so it is worth being exact.

- **`medfloat16`**: 16 bits. Top 4 bits = exponent, two's complement. Bottom 12 bits =
  mantissa, two's complement. Value = mantissa × 10^exponent.
- **`medfloat32`**: 32 bits. Top 8 bits = exponent, two's complement. Bottom 24 bits =
  mantissa, two's complement. Same formula.

Reserved special values, from GSS §2.1.1 Table 2.1 and PLXS §5.1 Table 5.1 — these are
compared against the **whole raw word**, never the mantissa:

| meaning | medfloat32 | medfloat16 |
|---|---|---|
| NaN (not a number) | `0x007FFFFF` | `0x07FF` |
| +INFINITY | `0x007FFFFE` | `0x07FE` |
| −INFINITY | `0x00800002` | `0x0802` |
| NRes (not at this resolution) | `0x00800000` | `0x0800` |
| Reserved | `0x00800001` | `0x0801` |

**The classic bug:** masking the mantissa first and comparing it to `0x07FF`. Under that
reading, `0x0FFF` — exponent 0, mantissa −1, i.e. the value **−1.0** — is misread as NaN,
and `0xF7FF` (exponent −1, mantissa −1, i.e. −0.1) likewise. `sfloat_check.py` asserts
`sfloat(0x0FFF) == -1.0` for exactly this reason.

**The second bug:** the exponent sign. `0xC037` is exponent −4, mantissa 55, = 0.0055. Read
the exponent as unsigned and you get 55 × 10^12. Both nibbles are two's complement and both
must be sign-extended.

Which type each field uses is not guessable and gets mixed up constantly:

| field | type | size |
|---|---|---|
| BP systolic / diastolic / MAP, pulse rate | `medfloat16` | 2 octets each |
| **Temperature measurement value** | **`medfloat32`** | **4 octets** |
| Glucose concentration | `medfloat16` | 2 |
| PLX SpO2, PR, pulse amplitude index | `medfloat16` | 2 each |
| **Weight, BMI, height** | **`uint16` with a fixed scale — not a float at all** | 2 each |
| **Heart rate value** | **`uint8` or `uint16` — not a float at all** | 1 or 2 |

### 1.4 Blood Pressure Measurement `0x2A35` — exact layout

GSS §3.34, Table 3.52.

```
octet 0      Flags                       boolean[8]
             bit 0  Units.  0 = mmHg, 1 = kPa
             bit 1  Time Stamp present
             bit 2  Pulse Rate present
             bit 3  User ID present
             bit 4  Measurement Status present
             bits 5-7  RFU
1..2         Systolic                    medfloat16   (mmHg if bit0=0, else kPa)
3..4         Diastolic                   medfloat16
5..6         Mean Arterial Pressure      medfloat16
[7..13]      Time Stamp                  Date Time, 7 octets   if bit 1
[..+2]       Pulse Rate                  medfloat16            if bit 2
[..+1]       User ID                     uint8 (0xFF = unknown) if bit 3
[..+2]       Measurement Status          boolean[16]           if bit 4
```

Minimum legal packet: **7 octets**. Fully populated: **19 octets** (asserted in
`sfloat_check.py`).

`Date Time` (GSS §3.79) is `uint16` year, then `uint8` month, day, hours, minutes, seconds.
**Year/month/day = 0 means "not known".** There is no time zone: it is local wall time on
the device.

Measurement Status bits (GSS §3.34.3) — these are the interesting clinical annotations:
bit 0 body movement, bit 1 cuff too loose, bit 2 irregular pulse detected, bits 3–4 pulse
rate range (`0b01` above upper limit, `0b10` below lower limit), bit 5 improper measurement
position.

`0x2A36` Intermediate Cuff Pressure is the same structure with the "systolic" slot carrying
the current cuff pressure and the other two set to NaN. It is inflation telemetry, not a
measurement — **do not store it as a reading**.

### 1.5 Temperature Measurement `0x2A1C` — exact layout

GSS §3.238, Table 3.364.

```
octet 0      Flags     bit 0  Units.  0 = Celsius, 1 = Fahrenheit
                       bit 1  Time Stamp present
                       bit 2  Temperature Type present
                       bits 3-7  RFU
1..4         Temperature Value           medfloat32   (4 octets, not 2)
[5..11]      Time Stamp                  Date Time, 7 octets   if bit 1
[..+1]       Temperature Type            uint8                 if bit 2
```

Minimum 5 octets, maximum 13. `VitalKind.BODY_TEMPERATURE` is °C, so a device with bit 0
set must be converted: `C = (F − 32) × 5/9`. Getting this wrong is the failure mode named in
the brief. It is at least *loud*: 97.7 °F read as Celsius is outside `PLAUSIBLE` (30–45) and
gets rejected. That is luck, not design — see §1.8.

### 1.6 Glucose Measurement `0x2A18` — exact layout, and a spec contradiction

GSS §3.118, Table 3.190.

```
octet 0      Flags     bit 0  Time Offset present
                       bit 1  Glucose Concentration AND Type-Sample Location present
                       bit 2  Units
                       bit 3  Sensor Status Annunciation present
                       bit 4  Context Information follows (a 0x2A34 will arrive)
                       bits 5-7  RFU
1..2         Sequence Number             uint16
3..9         Base Time                   Date Time, 7 octets  (always present)
[10..11]     Time Offset                 sint16, minutes       if bit 0
[..+2]       Glucose Concentration       medfloat16            if bit 1
[..+1]       Type (low nibble) / Sample Location (high nibble)  uint8   if bit 1
[..+2]       Sensor Status Annunciation  boolean[16]           if bit 3
```

**The contradiction.** GSS 2026-02-05 §3.118.1 says bit 2: "0 = Glucose concentration in
units of mg/dL, 1 = … mmol/L". The field table *in the same document*, four lines earlier,
says bit 2 = 0 → `org.bluetooth.unit.mass_density.kilogram_per_liter` and bit 2 = 1 →
`mole_per_litre`. Those are not the same claim and they differ by a factor of 10^5.

GLS 1.0.1 §3.1.1.5 settles it, and I am quoting it because this is the load-bearing
sentence for the whole glucose path:

> "If the unit of the Glucose Concentration is in base units of kg/L (typically displayed in
> units of mg/dL), bit 2 of the Flags field is set to 0. Otherwise, the unit is in base
> units of mol/L (typically displayed in units of mmol/L) and bit 2 of the Flags field is
> set to 1. … when a Glucose Concentration value in units of mg/dL is converted to units of
> kg/L, the SFLOAT exponent will need to be adjusted by subtracting 5. Similarly, when a
> Glucose Concentration value in units of mmol/L is converted to units of mol/L, the SFLOAT
> exponent will need to be adjusted by subtracting 3."

So the decoded float is in **kg/L or mol/L**, and:

- bit 2 = 0: `mg/dL = value × 1e5`, then `mmol/L = mg/dL ÷ 18.0156`
- bit 2 = 1: `mmol/L = value × 1e3`

**Why this is nastier than it looks.** A device reporting 100 mg/dL encodes exponent −5,
mantissa 100. A lazy implementation that ignores the exponent and reads the mantissa gets
`100` — which is *the right number in mg/dL*. It looks correct. Feed the same code a mmol/L
device reporting 5.5 mmol/L (exponent −4, mantissa 55) and it returns `55`. `PLAUSIBLE` for
`BLOOD_GLUCOSE` is (1.0, 40.0), so 55 is rejected and you notice. But a device reporting
3.0 mmol/L gives mantissa 30 — inside the range, silently wrong by 10×. `sfloat_check.py`
asserts the naive-mantissa coincidence explicitly so nobody "simplifies" the decoder later.

### 1.7 Weight, Pulse Oximeter and Heart Rate — exact layouts

**Weight Measurement `0x2A9D`** (GSS §3.275). **The flag bit order is different from blood
pressure and this is a trap:**

```
octet 0      Flags     bit 0  Units.  0 = SI (kg, m), 1 = Imperial (lb, in)
                       bit 1  Time Stamp present
                       bit 2  User ID present          <- BP uses bit 2 for Pulse Rate
                       bit 3  BMI AND Height present   <- BP uses bit 3 for User ID
1..2         Weight    uint16.  × 0.005 kg  (SI)  or  × 0.01 lb  (Imperial)
[3..9]       Time Stamp   Date Time, 7 octets   if bit 1
[..+1]       User ID      uint8                 if bit 2
[..+2]       BMI          uint16 × 0.1 kg/m²    if bit 3
[..+2]       Height       uint16 × 0.001 m (SI) or × 0.1 in (Imperial)   if bit 3
```

`Weight == 0xFFFF` means **"Measurement Unsuccessful"** (WSS §3.2.1.2), and when used, all
optional fields except Time Stamp and User ID are disabled. Body Composition
(`0x181B`/`0x2A9C`) is a different, longer characteristic that may be split across
**continuation packets** when it exceeds the ATT MTU (BCS §3.2.1) — reassembly logic you do
not want in increment 1.

**PLX Spot-check `0x2A5E`** (PLXS §3.1, Table 3.2):

```
octet 0      Flags     bit 0 Timestamp present; bit 1 Measurement Status present;
                       bit 2 Device and Sensor Status present; bit 3 Pulse Amplitude
                       Index present; bit 4 Device Clock is Not Set
1..2         SpO2      medfloat16, percent
3..4         PR        medfloat16, bpm
[..7]  Timestamp   [..2] Measurement Status   [..3] Device+Sensor Status (24-bit)
[..2]  Pulse Amplitude Index
```

**PLX Continuous `0x2A5F`** (PLXS §3.2, Table 3.6) has a *different* flags meaning:
bit 0 SpO2PR-Fast present, bit 1 SpO2PR-Slow present, bit 2 Measurement Status, bit 3
Device+Sensor Status, bit 4 Pulse Amplitude Index. SpO2PR-Normal (2 × medfloat16) is always
present and there is no timestamp — it is live data, notified every 1–4 s.

If SpO2 or PR is unavailable the device sends **NaN** in that subfield (PLXS §3.1.1.2). A
decoder that does not check for NaN will store `float('nan')` as a vital.

**Heart Rate Measurement `0x2A37`** (GSS §3.125):

```
octet 0      Flags     bit 0  Value format: 0 = uint8, 1 = uint16
                       bit 1  Sensor Contact detected
                       bit 2  Sensor Contact supported
                       bit 3  Energy Expended present
                       bit 4  RR-Interval present
1 or 1..2    Heart Rate   uint8 or uint16, bpm
[..2]        Energy Expended   uint16
[..2n]       RR-Intervals      uint16[n], unit 1/1024 s, oldest first
```

Two notes. **Bit 1 is meaningless unless bit 2 is set** — "sensor contact not detected" and
"this sensor has no contact detection" are both encoded as bit1=0. And **the number of
RR-intervals is inferred from the remaining packet length**, so mis-reading bit 0 shifts
every RR value by one octet and produces garbage that still parses. GSS 2026 gives Energy
Expended's unit as joule while HRS 1.0 said kilojoule; we do not use the field, but do not
trust it if someone later wants it.

### 1.8 The traps, and which ones `PLAUSIBLE` catches

Measured in `sfloat_check.py` against the real ranges from
`packages/protocol/src/ars_protocol/health.py`:

| bug | wrong value produced | caught by `PLAUSIBLE`? |
|---|---|---|
| Fahrenheit stored as Celsius | 97.7 for 36.5 °C | ✅ rejected (range 30–45) |
| kPa stored as mmHg | 16.0 for 120 mmHg | ✅ rejected (range 50–260) |
| mg/dL stored as mmol/L | 100.0 | ✅ rejected (range 1–40) |
| uint16 HR misparsed | 300 bpm | ✅ rejected (range 20–250) |
| **mmol/L stored as mg/dL** | **5.5 → looks like a normal mmol/L** | ❌ **accepted silently** |
| **pounds stored as kilograms** | **176.37 for 80 kg** | ❌ **accepted silently** |
| **weight sentinel `0xFFFF` decoded** | **327.675 kg (SI) / 297.262 kg (Imperial)** | ❌ **accepted silently** |

`PLAUSIBLE` is a good net and it is doing real work here — four of seven. It cannot catch
the other three, because in those cases the *wrong* number is a physically possible one.
Those three need explicit guards in the decoder, not in the range check:

1. Refuse the weight sentinel before any scaling: `if raw == 0xFFFF: no reading`.
2. Convert on the units bit unconditionally; never assume SI or mg/dL.
3. **Persist the raw flags octet with every reading** (in `note` or, better, a structured
   field — see §5.3) so a units bug found in six months is retro-diagnosable rather than a
   year of corrupted history.

---

## 2. Which real devices actually implement this

Standards conformance in this market is **worse than the existence of the specs suggests**.
The pattern: professional / telehealth-channel devices implement the SIG profiles because
Continua certification requires it; consumer devices sold on an app experience implement
whatever their app needs.

Confidence column: **verified** = a primary source or working open-source implementation
names the UUIDs; **strong** = vendor documentation or a certification claim implies it;
**reported** = community sources agree but I did not see the wire.

| device | category | speaks standard GATT? | confidence | notes |
|---|---|---|---|---|
| **A&D UA-651BLE** | BP | **Yes — `0x1810`/`0x2A35`** | verified | Continua Certified. A user's `0x1810`/`0x2A35` access is on record in [cordova-plugin-ble-central#27](https://github.com/don/cordova-plugin-ble-central/issues/27). Pairing: hold Start until "Pr", 60 s window. **Buy this one.** |
| **A&D UT-201BLE** | Thermometer | **Yes** (HTS) | strong | [Continua Certified](https://medical.andprecision.com/product/ut-201ble-precision-digital-thermometer-with-bluetooth-smart-bluetooth-low-energy-connectivity/); only clearly standards-based consumer thermometer I found |
| **A&D UC-352BLE** | Scale | **Yes** (WSS) | strong | [Continua Certified](https://medical.andprecision.com/product/uc-352ble-precision-health-scale-with-bluetooth-smart-bluetooth-low-energy-connectivity/). Still subject to the single-client problem, §2.2 |
| **Beurer BC54, BM54, BM64, BM81** | BP | **Yes — `0x1810`/`0x2A35`** | verified | Listed under the *Generic Bluetooth* plugin in [ubpm](https://codeberg.org/LazyT/ubpm/wiki/Plugins), which explicitly matches on `0x1810`/`0x2A35`. **6-digit PIN pairing.** |
| **Hartmann BPW26 (Compact+)** | BP | Yes, new readings only | reported | ubpm generic plugin, 6-digit PIN |
| **Nonin 3230** | SpO2 | **Yes — both** | verified | Its [operator's manual](https://www.nonin.com/wp-content/uploads/Operators-Manual_8.5x11_Model-3230_English.pdf) p.13: "Bluetooth Profiles Supported: GATT-based Nonin Proprietary Oximeter Profile; **GATT-based Bluetooth SIG Pulse Oximeter Profile**" and "Compliant with Bluetooth SIG Pulse Oximeter Profile specifications adopted by Continua." **The only unambiguous standards-compliant consumer oximeter I found.** |
| **Polar H10 / H9** | HR strap | **Yes — `0x180D`/`0x2A37`** | strong | [Polar's own SDK docs](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/products/PolarH10.md); raw ECG is behind Polar's separate proprietary PMD service, which we do not need. **One BLE connection at a time.** |
| **Garmin HRM-Dual / HRM-Pro** | HR strap | Yes (BLE HR profile + ANT+) | reported | Garmin markets simultaneous ANT+ and BLE to third-party apps (Zwift etc.); [Garmin's own spec page](https://www8.garmin.com/manuals/webhelp/hrm-dual/EN-US/GUID-ACE86E1D-9499-4D84-9BC2-00C4F06B0614.html) does not name the profile, so this is inference from ecosystem behaviour, not a vendor statement |
| **Ascensia Contour Next One** | Glucose | **Yes** (`0x1808`, `0x2A18`, `0x2A34`, RACP `0x2A52`) | verified | [GlucometerBluetoothToHealthKit](https://github.com/LiamsGitHub/GlucometerBluetoothToHealthKit) is a working standard-GLS client built against this exact meter |
| **Omron HEM-7361T (M7 Intelli IT)** | BP | **Partly.** Exposes `0x1810`/`0x2A35` for *new* readings; stored history is proprietary | verified | ubpm lists it under the generic plugin, "only new", pairing "special" |
| **Omron HEM-7150T / 7155T / 7322T / 7342T / 7380T1 / 7377T1 / 7530T / 7600T (Evolv) / 6232T** | BP | **No** for history — custom service `ecbe3980-c9a2-11e1-b1bd-0002a5d5c51b` with RX/TX channels, an unlock characteristic `b305b680-…` and a 16-byte pairing key, driving EEPROM reads | verified | [omblepy](https://github.com/userx14/omblepy) source. **HEM-7196T is encrypted and unsupported even there.** |
| **Beurer GL50 evo** | Glucose | **No** — needs a separate Bluetooth adapter accessory; protocol reportedly SCSI-encapsulated and not fully reverse-engineered | reported | [Flameeyes' review](https://flameeyes.blog/2019/08/10/glucometer-review-beurer-gl50-evo/) |
| **Wellue / Viatom O2Ring, Checkme O2** | SpO2 | **No** — custom service `14839ac4-7d7e-415c-9a42-167340cf2339`, `0xAA`-framed packets with CRC-8, file download commands | verified | [wellue-o2ring-protocol](https://github.com/farolone/wellue-o2ring-protocol) |
| **Contec CMS50D-BT and siblings** | SpO2 | **No** — proprietary serial-over-BLE | reported | No standards claim in any Contec material I could find |
| **Withings BPM Connect, Body / Body+ scales** | BP, scale | **No** — Withings proprietary protocol, end-to-end encrypted to Withings' servers; the supported integration is the [Withings cloud Data API](https://developer.withings.com/api-reference/) | strong | Cloud-only. Fails the local-first premise outright. |
| **Xiaomi / Renpho / Yunmai / Medisana / Soehnle / Sanitas scales** | Scale | **No** — per-vendor proprietary, some with encrypted MiBeacon frames, some requiring vendor-app account registration | reported | [openScale](https://github.com/oliexdev/openScale) and [BLE Scale Sync](https://blescalesync.dev/guide/supported-scales) both carry one adapter per brand plus a single "any standard BT SIG (BCS/WSS)" catch-all — which names no models |

### 2.1 Which are not worth supporting, plainly

- **Withings anything.** The data path is Withings' cloud. Supporting it means A.R.S talks
  to a vendor server about Alex's blood pressure. That contradicts the first line of
  `CLAUDE.md`. Skip it — including the OAuth API route.
- **Omron's stored-record protocol.** `omblepy` is good work, but it is a
  reverse-engineered EEPROM layout per model, with a device-specific unlock key, and the
  newest model is already encrypted and unreadable. A firmware update can silently change a
  field offset, and the failure mode of a silently-changed offset in a blood-pressure
  decoder is a wrong number presented as fact. If Alex already owns an Omron, use its
  `0x1810` path for *live* readings only and type history in by hand
  (`DeviceKind.MANUAL` exists precisely for this).
- **Contec, Wellue, Berry-class oximeters.** Cheap, proprietary, and the community protocol
  docs are per-firmware. Buy the Nonin if SpO2 matters; otherwise skip the category.
- **Consumer body-composition scales.** Body fat percentage from a foot-to-foot bioimpedance
  scale has no `VitalKind` and no defensible reference range, so A.R.S could not say
  anything about it anyway without inventing a `Condition`-shaped type that `health.py`
  deliberately refuses to have.
- **Beurer GL50 evo.** Needs an accessory dongle and the protocol is not open. If glucose is
  ever needed, buy the Contour Next One.

### 2.2 The weight-scale structural problem

WSS §3.2.1 is explicit and it is a design constraint, not a bug:

> "Once transfer of a measurement is successful, the measurement shall not be retransmitted.
> As such, the design of this Service is only suited to sending indications to a single
> Client for a given user."

A scale hands each reading to exactly one collector and then forgets it. If Alex's phone app
is also bonded, whichever connects first wins and A.R.S gets nothing — non-deterministically,
day to day. The same "once, to one client" language appears in BCS. This is why weight ranks
last on value-for-effort in §5.5 despite `VitalKind.WEIGHT` being trivially easy to decode.

---

## 3. The Python layer on macOS

`bleak` is the right choice and there is no serious competitor for CoreBluetooth from
Python. Everything below was read out of the installed `bleak` 3.0.2 source tree or its
documentation, not guessed.

### 3.1 Does the CoreBluetooth backend handle Indicate?

**Yes, transparently, and this is the single most important compatibility question** given
that four of six profiles are Indicate-only.

`bleak/backends/corebluetooth/PeripheralDelegate.py:474` calls
`self.peripheral.setNotifyValue_forCharacteristic_(True, characteristic)`. CoreBluetooth's
`setNotifyValue:` selects notify or indicate from the characteristic's own declared
properties, so `await client.start_notify(...)` subscribes to `0x2A35` correctly. There is
no separate `start_indicate`, and you should not look for one.

### 3.2 macOS permission prompts: when, and attributed to what

- The framework requires **`NSBluetoothAlwaysUsageDescription` in the Info.plist of the
  *bundle*** that uses Bluetooth. Without it the process is killed by TCC — bleak's
  troubleshooting page documents the exact crash: `Termination Reason: Namespace TCC,
  Code 0`, `EXC_CRASH (SIGABRT)`.
- The prompt appears **on first Bluetooth use**, not at import.
- If the user denies it, bleak raises `BleakBluetoothNotAvailableError` on every subsequent
  access, and **re-requesting programmatically is impossible** — recovery is only via
  System Settings → Privacy & Security → Bluetooth.

**The practical consequence for A.R.S.** A bare `uv run python …` has no bundle and no
Info.plist. In that configuration the grant attaches to the *terminal application*, which
means (a) Alex grants Bluetooth to Terminal/iTerm rather than to A.R.S, which is both wrong
and too broad, and (b) the behaviour differs the moment A.R.S runs as a packaged app.
**The BLE reader must run inside a bundled `.app` with its own Info.plist** so the grant is
A.R.S's own and revocable independently. That is a packaging requirement, not a code one,
and it should be decided before the first line of the backend is written.

I did **not** instantiate a `CBCentralManager` to observe the prompt, because doing so would
put a system permission dialog on Alex's screen and mutate his TCC state — outside the brief.
**What would settle it:** build a throwaway `.app` with `NSBluetoothAlwaysUsageDescription`,
run it once, and check whether the entry that appears in Privacy & Security → Bluetooth is
the app or the terminal.

### 3.3 Pairing and bonding

**`BleakClient.pair()` raises `NotImplementedError` on macOS** — literally, at
`backends/corebluetooth/client.py:205`: `raise NotImplementedError("Pairing is not
available in Core Bluetooth.")`. Passing `pair=True` to the constructor is silently ignored
(`client.py:93`: "Explicit pairing is not available in CoreBluetooth").

This is not a blocker; it changes the shape of the code. macOS **auto-pairs**: the OS raises
the system pairing dialog the first time you touch a characteristic that requires
authentication, and the GATT operation blocks until the user answers. From bleak's macOS
backend docs:

> "macOS will prompt the user the first time a characteristic that requires
> authorization/authentication is accessed. This means that a GATT read or write operation
> could block for a long time waiting for the user to respond. So timeouts should be set
> accordingly."

Concretely: the A&D UA-651BLE wants "Pr" mode entered by hand and gives you 60 seconds; the
Beurer models want a 6-digit PIN typed into a macOS dialog. So the *first* `start_notify` on
`0x2A35` can take tens of seconds and needs a human at the keyboard. Hence the deliberately
generous 120 s default on `DeviceBackend.read` in §5.1, and hence a first-run flow that says
out loud, in EN and RO, "press and hold Start until the display shows Pr, then enter the PIN
macOS is about to ask for."

### 3.4 Device identity on macOS is not a MAC address

`backends/corebluetooth/scanner.py:162`: `address = peripheral.identifier().UUIDString()`.
`BLEDevice.address` on macOS is a **CoreBluetooth peripheral UUID, generated per Mac**. The
same cuff has a different "address" on Alex's laptop than on his phone or on a rebuilt
machine. There is a `cb={"use_bdaddr": True}` option that calls the undocumented
`retrieveAddressForPeripheral_`, which bleak's own docstring flags as liable to break on
future macOS releases.

**This collides with a docstring in the protocol.** `ReadingSource.device_id` says "Stable
per physical device, so 'my old cuff read 10 mmHg high' is expressible." A CoreBluetooth
UUID is not that. Design consequence in §5.3.

Also: **`scanning_mode="passive"` raises `BleakError` on macOS** (`scanner.py:90`), and on
macOS 12.0–12.2 scanning returns nothing unless `service_uuids` is supplied. Always pass
`service_uuids=[…]` — it is correct on every version, it is required on some, and it keeps
every other BLE device in the building out of the log.

### 3.5 When the device disappears mid-session

Read out of `backends/corebluetooth/client.py:114–130`. On disconnect, bleak:

1. clears the cached service collection (so a reconnect re-discovers rather than reusing
   stale handles);
2. **sets `BleakError("disconnected")` as the exception on every pending delegate future** —
   so an in-flight `read_gatt_char` or `start_notify` raises rather than hanging forever;
3. invokes the user's `disconnected_callback`.

There is **no automatic reconnection**. Nothing retries. And a notification callback that
never fires again produces no exception at all — it just goes quiet.

So a correct session loop needs both a disconnect signal *and* its own deadline. The sketch
in §3.6 pushes a `None` sentinel onto the queue from `disconnected_callback` and wraps the
queue read in `asyncio.wait_for`. This matters more than it sounds: several of these devices
*deliberately* drop the link right after delivering their reading. PLXS §3.1.1 says a spot-
check oximeter "may end the connection once the new Spot-check measurement has been
indicated". A backend that treats disconnect as an error will report a failure on every
successful measurement.

One more, from bleak's own docs: **"Calling `asyncio.run()` more than once"** breaks it.
Bleak needs one continuously-running loop for its background tasks. The A.R.S service must
own the loop; the device backend must not create one.

### 3.6 Minimal correct code: subscribe to a blood pressure measurement

Full runnable version, with the mock and the `PLAUSIBLE` filtering, at
`/Users/alexandrustratulat/ars/research/experiments/ble/device_backend_sketch.py`. The two
load-bearing parts:

```python
_SFLOAT_SPECIAL = {0x07FF: float("nan"), 0x0800: float("nan"), 0x0801: float("nan"),
                   0x07FE: float("inf"), 0x0802: float("-inf")}

def sfloat(raw: int) -> float:
    """medfloat16. `raw` is the 16-bit word already read little-endian."""
    if raw in _SFLOAT_SPECIAL:        # compare the WHOLE word, never the mantissa:
        return _SFLOAT_SPECIAL[raw]   # 0x0FFF is -1.0, not NaN.
    exp, mant = raw >> 12, raw & 0x0FFF
    if exp >= 0x8:                    # 4-bit two's complement
        exp -= 0x10
    if mant >= 0x800:                 # 12-bit two's complement
        mant -= 0x1000
    return mant * (10.0 ** exp)
```

and the subscribe:

```python
async with BleakClient(
    candidate.transport_id,
    disconnected_callback=on_disconnect,      # pushes None onto the queue
    services={BLOOD_PRESSURE_SERVICE},
    timeout=timeout_s,
) as client:
    # start_notify covers Indicate too: CoreBluetooth's setNotifyValue: picks notify
    # or indicate from the characteristic's own properties. 0x2A35 is Indicate-only.
    #
    # The discriminator is macOS-specific: CoreBluetooth delivers reads and
    # notifications through the same delegate callback. We never read 0x2A35, so
    # anything arriving on it is an indication.
    await client.start_notify(
        BLOOD_PRESSURE_MEASUREMENT, on_indication,
        cb={"notification_discriminator": lambda _d: True},
    )
```

That `notification_discriminator` is not decoration. `PeripheralDelegate.py:580–592`:
CoreBluetooth routes both read responses and notifications through
`didUpdateValueForCharacteristic:`, and bleak's own comment says that without a
discriminator, when a read is pending it "assumes it is a read response but can't know for
sure". Because we never read `0x2A35`, `lambda _d: True` is exactly right, and stating it
explicitly documents the assumption.

The decode is in `decode_blood_pressure()` in that file; the shape that matters is that it
**raises `ValueError` on a short or over-long packet rather than returning a partial
result**, and that it yields two or three separate `VitalReading` objects (systolic,
diastolic, and pulse when present) rather than one compound record — because `VitalKind` is
scalar and that is the right call.

Verified output of the sketch, decoding five constructed packets:

```
candidate: MOCK-BP @ MOCK-0001 kind=blood_pressure_monitor
  bp_systolic     120.00 mmHg   at 2026-09-07T08:15:00  note='irregular pulse detected'
  bp_diastolic     80.00 mmHg   at 2026-09-07T08:15:00  note='irregular pulse detected'
  heart_rate       62.00 bpm    at 2026-09-07T08:15:00  note='irregular pulse detected'
  bp_systolic     120.01 mmHg   ...   (kPa device, converted)
  bp_diastolic     80.26 mmHg   ...
  heart_rate       58.00 bpm    ...
  bp_systolic     118.00 mmHg   ...   (minimal 7-octet packet, no pulse field)
  bp_diastolic     77.00 mmHg   ...
  bp_systolic     131.00 mmHg   ...   note='device clock unset; timestamped on arrival'
  bp_diastolic     84.00 mmHg   ...
  heart_rate       70.00 bpm    ...

11 readings, expected 11: both halves of the 20/10 packet were dropped by PLAUSIBLE (ok)
short packet correctly rejected: blood pressure measurement too short: 3 octets
```

---

## 4. Apple Health

**Short answer: no. Not from Python, and not from Swift either, because there is no health
store on a Mac.** This is measured, not inferred, and it is the opposite of what the
framework's presence suggests.

### 4.1 What is actually on the machine

HealthKit *is* present on macOS and has been since Ventura. Both of these are on this
machine right now:

```
/System/Library/Frameworks/HealthKit.framework
/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/HealthKit.framework
```

with 95 public headers, and `HKHealthStore.h:37` declares
`API_AVAILABLE(ios(8.0), watchos(2.0), macCatalyst(13.0), macos(13.0))`. So it compiles, it
links, and pyobjc loads it fine.

### 4.2 What it does when you call it

Measured today via pyobjc, loading the framework with `objc.loadBundle` (no radio, no data
access):

```
isHealthDataAvailable: False
supportsHealthRecords: False
```

Apple's own header, four lines below the declaration: "Using HKHealthStore APIs on devices
which are not supported will result in errors with the `HKErrorHealthDataUnavailable` code.
Call `isHealthDataAvailable` before attempting to use other parts of the framework."

**`False` means there is no Health database on this Mac.** Alex's blood pressure history
lives on his iPhone and is not synced to the Mac in any form HealthKit can see.

### 4.3 A second, independent blocker

Even ignoring the above, requesting authorization from a bare Python process fails before it
starts. Measured:

```
ValueError: NSInvalidArgumentException - NSHealthShareUsageDescription must be set in the
app's Info.plist in order to request read authorization for the following types:
HKQuantityTypeIdentifierBloodPressureDiastolic, HKQuantityTypeIdentifierBloodPressureSystolic
```

A `uv run python` process has no bundle and therefore no Info.plist. (Note in passing that
HealthKit expanded a systolic request into the diastolic type too — blood pressure is a
correlation there, which is a useful precedent for §5.3.) Also worth recording as a Python-
layer fact: pyobjc has **no bridge metadata for HealthKit**, so any method taking a
completion block raises `TypeError: Argument 4 is a block, but no signature available` until
you hand-write an `objc.registerMetaDataForSelector` call. Even in the world where HealthKit
worked on macOS, the Python route would be hand-rolled ObjC bridging, not a library.

### 4.4 The real options, ranked

| option | works? | effort | verdict |
|---|---|---|---|
| Python → HealthKit on macOS | **No.** `isHealthDataAvailable == False` | — | Dead. Not a Python limitation; there is no data. |
| Swift helper on macOS | **No.** Same store, same `False` | — | Dead for the same reason. A Swift helper solves the bridging problem, not the missing-database problem. |
| **iOS companion app** writing to A.R.S | Yes | High — a real app, provisioning profile, `NSHealthShareUsageDescription`, background delivery via `HKObserverQuery`, plus a sync channel to the Mac | The only route to steps, sleep, and everything Alex's watch and phone already collect. Correct eventually; wrong for increment 1. |
| **Manual Health export** (`export.zip` → `export.xml`) | Yes, and Python parses it fine | Low to build, but the *user* work is manual every time | Useful as a one-off backfill of history. [Apple's instructions](https://support.apple.com/guide/iphone/share-your-health-data-iph5ede58c3d/ios): Health → profile → Export All Health Data. Unfiltered, all-time, frequently hundreds of MB. Not a sync mechanism. |
| **Direct BLE** | Yes, today, on this machine | Medium | **Do this.** |

The uncomfortable framing worth saying out loud: for an Omron or Withings device, the
*vendor app on the iPhone* → HealthKit → iOS companion path would get better data with less
protocol work than reverse-engineering the device. It is the right long-term answer for
those specific devices. It is simply not available from a Mac, and it costs an iOS app.

---

## 5. What this means for the design

### 5.1 The `DeviceBackend` seam

Proposed for `packages/core/src/ars_core/interfaces.py`, satisfying `CLAUDE.md` rule 2.
Full runnable version with a BLE implementation and a mock in
`research/experiments/ble/device_backend_sketch.py`.

```python
class DeviceBackend(ABC):
    """A source of VitalReadings from physical equipment."""

    @property
    @abstractmethod
    def supported_kinds(self) -> frozenset[VitalKind]: ...

    @abstractmethod
    def discover(self, *, timeout_s: float = 10.0) -> AsyncIterator[DeviceCandidate]: ...

    @abstractmethod
    def read(self, candidate: DeviceCandidate, *,
             timeout_s: float = 120.0) -> AsyncIterator[VitalReading]: ...

    @property
    @abstractmethod
    def is_standard_profile(self) -> bool: ...
```

Design notes, each of which is a decision rather than a detail:

- **`read` and `discover` are declared `def`, not `async def`.** Same reasoning as
  `LlmBackend.complete` and `TranslationEngine.translate` already in `interfaces.py`: an
  async generator function already returns an `AsyncIterator`, and declaring the seam
  `async def` forces every call site into `async for x in await backend.read(...)`.
- **There is no `write`, no `configure`, no `set_time`.** The type cannot express sending
  anything to a medical device. That is a safety property enforced by the signature.
- **`supported_kinds` must list only kinds actually decoded from a real device**, not kinds
  the profile theoretically permits. A backend that overstates this lies to the router.
- **`is_standard_profile`** exists so the UI can say which readings came from an adopted
  profile and which from a reverse-engineered guess. If a vendor backend is ever added, this
  is what stops it from being indistinguishable from the real thing.
- **The 120 s default on `read` is deliberate** — a cuff takes ~40 s, and the first
  connection blocks on a macOS pairing dialog a human has to answer (§3.3).

Two implementations, both in the sketch: `BleDeviceBackend` (the **only** place in A.R.S
permitted to `import bleak`, done inside `__init__`) and `MockDeviceBackend` (replays packet
bytes; what every test uses; no radio, no `bleak` dependency, deterministic).

### 5.2 The guard and capability layer needs no changes

Checked, and this is good news: `packages/protocol/src/ars_protocol/capability.py` already
has everything.

- `Capability.DEVICE_CONNECT` = `device.connect`, `Risk.MEDIUM` — for `discover`/`read`.
- `Capability.HEALTH_WRITE` = `health.write`, `Risk.CRITICAL` — for storing the readings.
- `Capability.HEALTH_READ` = `health.read`, `Risk.HIGH` — for anything that reads them back.
- EN, RO **and DE** prompt strings already exist in `services/auth/src/ars_auth/messages.py`
  ("connect to a medical device" / "mă conectez la un dispozitiv medical" / "mich mit einem
  medizinischen Gerät verbinden"). Rule 5 is already satisfied for the consent path.

`VitalReading.sensitivity` defaults to `Sensitivity.SENSITIVE`, and `Router._guard_cloud`
already refuses SENSITIVE content to non-local backends, so a device reading cannot reach a
cloud LLM without a deliberate change. The skill wrapping `DeviceBackend` calls
`GuardEngine.evaluate` for `DEVICE_CONNECT` before `discover`, and the runtime re-checks
before `read` — rule 3, defence in depth.

### 5.3 Three protocol changes needed in `packages/protocol`

Per rule 1, these belong in `health.py`, not in a service.

**(a) `DeviceCandidate`.** A discovered-but-not-trusted device needs a type. Sketched in the
experiment file: `transport_id`, `name`, `device_kind`, `services`, `rssi`. The docstring
must say that `transport_id` is backend-scoped and, on CoreBluetooth, per-Mac.

**(b) `ReadingSource.device_id` needs a stability fix — this is a latent bug.** The
docstring promises "Stable per physical device". On macOS the natural candidate is the
CoreBluetooth peripheral UUID, which is **per-Mac** (§3.4). A.R.S targets Mac *and later iOS
and Android*, so the same cuff would produce three different `device_id`s and "my old cuff
read 10 mmHg high" becomes inexpressible — the exact capability the docstring claims. Fix:
`device_id` is an A.R.S-assigned identifier (`new_id("dev")`) minted once when the user
confirms a device, with per-host transport identifiers stored as aliases on a device record.
Do this before the first reading is written, because it is not retrofittable.

**(c) The `note` field is user-facing and currently monolingual.** The sketch writes
`note="irregular pulse detected"` — English, from a decoder, into a field a bilingual
assistant will read aloud. That is a rule 5 violation the moment it ships. Fix: carry the
Measurement Status as a structured value (a `frozenset` of an enum, or the raw `uint16`),
and render EN/RO/DE at the presentation layer where every other user-facing string already
lives. Storing the raw flags octet also gives the retro-diagnosis property from §1.8 for
free.

**What I recommend *not* adding:** a `VitalKind` for Mean Arterial Pressure. Every `0x2A35`
carries it, and dropping it is the only data loss in the pipeline — but nothing consumes
MAP, no reference range in A.R.S cites it, and `VitalKind` is "deliberately closed" for good
stated reasons. Adding an enum value nothing reads is how closed enums stop being closed.
Revisit if a reference source that uses MAP is ever added.

### 5.4 Which `VitalKind`s are reachable

| `VitalKind` | reachable in increment 1? | how |
|---|---|---|
| `BLOOD_PRESSURE_SYSTOLIC` | **Yes** | BLS `0x2A35`, A&D UA-651BLE or Beurer BM64 |
| `BLOOD_PRESSURE_DIASTOLIC` | **Yes** | same indication |
| `HEART_RATE` | **Yes** | free from the same BP indication (pulse field), and from HRS `0x2A37` on a Polar H10 |
| `SPO2` | Later — profile is ready, hardware is the constraint | PLXS `0x2A5E`; needs a Nonin 3230 |
| `BODY_TEMPERATURE` | Later | HTS `0x2A1C`; A&D UT-201BLE. Cheap to add once BLS works — same shape, different float width |
| `WEIGHT` | Later, low priority | WSS `0x2A9D`, but see §2.2 |
| `BLOOD_GLUCOSE` | Later, highest effort | GLS + RACP state machine + bonding; Contour Next One |
| `RESPIRATORY_RATE` | **No** | No adopted GATT profile in any of these six categories emits it |
| `STEPS` | **No** | Watch/phone data. Needs the iOS companion (§4.4) |
| `SLEEP_MINUTES` | **No** | Same |

So increment 1 covers **3 of 10 kinds with one device**, and the marginal cost of the fourth
(`BODY_TEMPERATURE`) is small once the float and flags machinery exists.

### 5.5 Value for effort

Effort is my **estimate**, in engineer-days, not a measurement. Hardware prices are
approximate street prices in EUR and were not verified against a retailer today.

| profile | value | effort (est.) | hardware (est.) | ratio | do it? |
|---|---|---:|---|---|---|
| **Blood Pressure `0x1810`** | High — the number Alex most plausibly wants tracked; 3 `VitalKind`s at once; the `PLAUSIBLE` net works for it | **3 d** | ~€60 A&D UA-651BLE | **best** | **Yes, first** |
| **Heart Rate `0x180D`** | Medium — trivial decode, strap likely already owned, gives a live-data path to exercise the seam | **1 d** | €0–90 | **best** | **Yes, second** |
| Health Thermometer `0x1809` | Medium — the one reading you actually want when ill | 1.5 d (mostly float32 + °F) | ~€45 A&D UT-201BLE | good | Increment 2 |
| Pulse Oximeter `0x1822` | Medium | 2.5 d (spot + continuous are different layouts) | ~€250 Nonin 3230 — the blocker | fair | Increment 2, only if the Nonin is bought |
| Weight Scale `0x181D` | Low — single-client race with the phone app (§2.2), thin standards-compliant market | 1.5 d | ~€90 A&D UC-352BLE | poor | Defer |
| Glucose `0x1808` | Situational — high if Alex needs it, zero otherwise | **5 d** (RACP state machine, bonding, sequence-number dedup, unit trap §1.6) | ~€30 Contour Next One | poor unless needed | Defer; ask first |
| Omron proprietary | Low — one vendor, per-model EEPROM maps, newest model already encrypted | 8 d+ and never finished | — | **negative** | **No** |
| Withings cloud API | Negative — sends Alex's vitals to a vendor server | 3 d | — | **negative** | **No** |
| iOS companion → HealthKit | High eventually — the only route to steps/sleep/watch data | 10 d+ | iPhone + dev account | later | Not now |

### 5.6 Definition of done for increment 1

- `DeviceBackend` in `packages/core/interfaces.py`; `DeviceCandidate` and the `device_id`
  fix in `packages/protocol/health.py`.
- `BleDeviceBackend` (BLS + HRS) in a service, the only `import bleak` in the tree.
- `MockDeviceBackend` plus the packet corpus from `sfloat_check.py` as unit tests, including
  every trap in §1.8 as a regression test.
- EN/RO/DE strings for the pairing flow ("hold Start until Pr appears"), for the Measurement
  Status annotations, and for the "device disconnected before it reported" case.
- Telemetry: readings decoded, readings dropped by `PLAUSIBLE` (with kind and raw value),
  decode errors, disconnects, time-to-first-reading.
- A line in `docs/architecture/overview.md` for the new service boundary.

**What I would skip, and say so:** intermediate cuff pressure `0x2A36` (inflation telemetry,
not measurements), the BLS 1.1.1 "Enhanced"/RACP stored-record path (a history import, not a
reading path), Body Composition, and Glucose Measurement Context `0x2A34`.

---

## 6. Safety and privacy

### 6.1 Are these profiles encrypted? Mostly yes — and one is not

Every **service** specification says "Security Permissions: None" — the service imposes no
requirement. Security is set by the **profile**, and there the picture is clear but uneven:

| profile | requirement | quote |
|---|---|---|
| Blood Pressure (BLP 1.1.1 §6.1) | **Encryption mandatory** | "All supported characteristics specified by the Blood Pressure Service **shall** be set to Security Mode 1 and either Security Level 2 or 3." Sensor "should bond"; "should support LE Secure Connections" |
| Glucose (GLP 1.0.1 §6.1) | **Encryption mandatory** | identical Security Mode 1 / Level 2–3 language |
| Pulse Oximeter (PLXP 1.0.1 §7.1) | **Encryption mandatory, bonding mandatory** | "Security Mode 1 and Security Level 2 or higher. The Sensor **shall** bond with the Collector." |
| Heart Rate (HRP 1.0 §6.1) | **Conditional — and this is the gap** | "The Heart Rate Sensor **may** bond… **When** the Heart Rate Sensor uses bonding: [it] shall use LE Security Mode 1 and either Security Level 2 or 3." The Collector "shall support LE Security Mode 1 and Security **Levels 1**, 2 and 3." |

**So a heart-rate strap that does not bond is permitted to run at Security Level 1 — no
encryption at all.** That is the normal case for straps designed to work with any gym
machine or app, and it is why the standard warns Collectors to support Level 1. **Assume the
HR strap is broadcasting Alex's heart rate in the clear.**

Note also that LE Secure Connections (P-256 ECDH) is only a *should* in BLP, not a *shall*.
A device that supports only LE **Legacy** pairing negotiates its long-term key with a
scheme whose key exchange is recoverable by an attacker who captured the pairing exchange —
which is a one-time exposure at pairing, not a standing one, but it means the strength of
the link depends on the device, not the spec.

### 6.2 What a passive listener in the same room learns

Even against a properly encrypted, bonded blood pressure monitor:

- **Advertising data is never encrypted.** The 16-bit service UUID in the advertisement is
  `0x1810` — the SIG-assigned, publicly documented number for Blood Pressure. Anyone within
  radio range with a €20 dongle learns *there is a blood pressure monitor in this flat*.
  Same for `0x1808` (glucose — a stronger inference about a person than most medical records
  contain) and `0x1822` (pulse oximeter).
- **The advertised device name** is usually the model: `A&D_UA-651BLE_xxxx`, `Beurer BM64`.
  That is manufacturer and model, in the clear.
- **The device address.** Fitness and medical peripherals commonly use a static public or
  static random address rather than a Resolvable Private Address, because RPA requires the
  bonded peer to resolve it. A static address is a **permanent tracking identifier**. BLP
  1.1.1 §5.1.5's own Target Address AD Type discussion, and GLP 1.0.1's warning that
  "if the address is used for long periods of time it could be associated with the user",
  acknowledge exactly this.
- **Traffic analysis.** A 20-second connection at 08:05 every morning followed by one
  encrypted indication is a measurement, and its *existence and time* leak without any
  decryption. A run of three connections in ten minutes is a person taking a reading, not
  liking it, and taking it again.
- **The measurement values themselves are protected** on BP / glucose / SpO2 once bonded —
  AES-CCM link encryption. That part the standard gets right.

Net: **the values are confidential; the fact that Alex owns and regularly uses a blood
pressure monitor is not, and cannot be made so.** That is a property of BLE advertising, not
something A.R.S can fix. It is worth telling him plainly once, because it is the kind of
thing people assume "paired" solves.

### 6.3 On the host

- **Bond keys.** `/Library/Preferences/com.apple.Bluetooth.plist` is world-readable
  (`-rw-r--r-- root:wheel`) but on this machine is 321 bytes and contains no pairing material
  — just autoseek and audio preferences. Where macOS 26 actually stores LE long-term keys I
  **did not establish**; `/Library/Application Support/Bluetooth` and `/private/var/db/Bluetooth*`
  do not exist. *What would settle it:* `sudo lsof -p $(pgrep bluetoothd)` and a root-level
  search of `/private/var/db`. Worth doing before claiming anything about bond-key exposure
  in a security doc.
- **Data at rest is A.R.S's problem, and it is the bigger one.** A `VitalReading` series in
  the memory store is a far richer target than the radio link. `Sensitivity.SENSITIVE` by
  default plus the existing `_guard_cloud` refusal is the right architecture; rule 7 —
  deletion must actually delete, embeddings included — applies with full force here, and the
  retention test should cover vitals specifically, not just memory records.
- **Rule 8 and the corpus.** `research/experiments/ble/` contains only synthetic packets I
  constructed from the specifications. **No real reading of Alex's is in the repository and
  none should ever be committed** — the mock's fixtures must stay synthetic even when a real
  device is available and a real capture would be more convenient.

---

## 7. What would change these conclusions

- **A Health database appearing on macOS.** If a future macOS ships the Health app on the
  Mac, `isHealthDataAvailable()` flips to `True` and §4 is obsolete — HealthKit becomes
  strictly better than BLE for any device whose vendor app already writes there (Omron,
  Withings). *Test:* re-run the three-line pyobjc probe in §4.2 after each macOS update. It
  costs nothing and it is the single highest-leverage thing to re-check.
- **The A&D UA-651BLE not actually indicating `0x2A35` cleanly.** My evidence is a GitHub
  issue and a Continua certification, not a capture. *Settle it:* buy one, subscribe with
  the sketch, and confirm the first indication decodes to the number on the LCD. Until that
  happens, treat "buy A&D" as a well-supported recommendation, not a verified one.
- **The macOS Bluetooth permission being attributed to the terminal.** §3.2 predicts the
  grant lands on Terminal/iTerm for an unbundled process. *Settle it:* one throwaway `.app`
  with `NSBluetoothAlwaysUsageDescription`, then look at Privacy & Security → Bluetooth. If
  it turns out an unbundled process gets its own entry, the packaging requirement relaxes.
- **Omron firmware.** If Omron ever exposes stored records over `0x1810` + RACP, the "do not
  reverse-engineer" recommendation flips for anyone who already owns one. Unlikely; the
  trend in `omblepy`'s issue tracker is the opposite (HEM-7196T is now encrypted).
- **Alex needing glucose.** The 5-day estimate and the "defer" verdict assume he does not. If
  he does, glucose moves to first and the Contour Next One is the device, because it is the
  one meter with a known-working standard-GLS client.
- **A cheap standards-compliant pulse oximeter appearing.** The €250 Nonin is the entire
  reason SpO2 is deferred; the profile work is 2.5 days. Any sub-€100 device with a
  documented `0x1822` would move SpO2 up immediately. I looked and did not find one — that is
  a negative result and it is the honest state of the market as of today.
- **Any of the "reported"-confidence rows in §2.** Garmin's BLE profile, the Contec and
  Wellue protocol claims, and the Beurer thermometer situation all rest on community sources
  rather than vendor statements or captures. None of them change the recommendation, which
  is why I did not chase them further; if one becomes load-bearing, it needs a capture.

---

## Sources

Bluetooth SIG primary documents (downloaded and parsed 2026-09-07; version dates in §1.1):
[Assigned Numbers](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Assigned_Numbers/out/en/Assigned_Numbers.pdf) ·
[GATT Specification Supplement](https://btprodspecificationrefs.blob.core.windows.net/gatt-specification-supplement/GATT_Specification_Supplement.pdf) ·
[BLS 1.1.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/BLS_v1.1.1/out/en/index-en.html) ·
[BLP 1.1.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/BLP_v1.1.1/out/en/index-en.html) ·
[HTS 1.0](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HTS_v1.0/out/en/index-en.html) ·
[GLS 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/GLS_v1.0.1/out/en/index-en.html) ·
[GLP 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/GLP_v1.0.1/out/en/index-en.html) ·
[WSS 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/WSS_v1.0.1/out/en/index-en.html) ·
[BCS 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/BCS_v1.0.1/out/en/index-en.html) ·
[HRS 1.0](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRS_v1.0/out/en/index-en.html) ·
[HRP 1.0](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRP_v1.0/out/en/index-en.html) ·
[PLXS 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/PLXS_v1.0.1/out/en/index-en.html) ·
[PLXP 1.0.1](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/PLXP_v1.0.1/out/en/index-en.html) ·
[GATT Specification Supplement landing page](https://www.bluetooth.com/specifications/specs/gatt-specification-supplement/)

Python / macOS: [bleak macOS backend docs](https://bleak.readthedocs.io/en/latest/backends/macos.html) ·
[bleak troubleshooting](https://bleak.readthedocs.io/en/latest/troubleshooting.html) ·
bleak 3.0.2 source (`backends/corebluetooth/{client,scanner,PeripheralDelegate}.py`,
`args/corebluetooth.py`), read locally.

Apple: `HKHealthStore.h` from the macOS 26 SDK, read locally ·
[Share your data in Health on iPhone](https://support.apple.com/guide/iphone/share-your-health-data-iph5ede58c3d/ios)

Devices and implementations: [Nonin 3230 Operator's Manual](https://www.nonin.com/wp-content/uploads/Operators-Manual_8.5x11_Model-3230_English.pdf) ·
[Nonin 3230 product page](https://www.nonin.com/products/3230/) ·
[A&D UA-651BLE](https://medical.andprecision.com/product/ua-651ble-upper-arm-blood-pressure-monitor-with-bluetooth/) ·
[A&D UT-201BLE](https://medical.andprecision.com/product/ut-201ble-precision-digital-thermometer-with-bluetooth-smart-bluetooth-low-energy-connectivity/) ·
[A&D UC-352BLE](https://medical.andprecision.com/product/uc-352ble-precision-health-scale-with-bluetooth-smart-bluetooth-low-energy-connectivity/) ·
[ubpm plugin list](https://codeberg.org/LazyT/ubpm/wiki/Plugins) ·
[omblepy](https://github.com/userx14/omblepy) ·
[cordova-plugin-ble-central#27 (A&D UA-651BLE, 0x1810/0x2A35)](https://github.com/don/cordova-plugin-ble-central/issues/27) ·
[GlucometerBluetoothToHealthKit (Contour Next One, standard GLS)](https://github.com/LiamsGitHub/GlucometerBluetoothToHealthKit) ·
[wellue-o2ring-protocol](https://github.com/farolone/wellue-o2ring-protocol) ·
[openScale](https://github.com/oliexdev/openScale) ·
[BLE Scale Sync supported scales](https://blescalesync.dev/guide/supported-scales) ·
[Polar BLE SDK — H10](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/products/PolarH10.md) ·
[Garmin HRM-Dual specifications](https://www8.garmin.com/manuals/webhelp/hrm-dual/EN-US/GUID-ACE86E1D-9499-4D84-9BC2-00C4F06B0614.html) ·
[Withings Data API](https://developer.withings.com/api-reference/) ·
[Flameeyes on the Beurer GL50 evo](https://flameeyes.blog/2019/08/10/glucometer-review-beurer-gl50-evo/) ·
[A&D first Continua certified BP monitor (historical context)](https://www.mobihealthnews.com/3678/ad-medical-releases-first-continua-certified-blood-pressure-monitor-and-weight-scale)
