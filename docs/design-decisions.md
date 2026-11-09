# Design decisions in detail

The short version is the table in the [README](../README.md#design-decisions); this is the reasoning and the measurements behind each decision.

## Structure

- **One framework class, one module per concern.** `AmmeterTestFramework` (`src/testing/`) runs tests and
  coordinates; statistics, plotting, accuracy (`src/analysis/`) and archiving (`src/results/`) are independent
  modules that work on plain result dicts. Each can be used and tested on its own, including on archived records.
- **Configuration-driven, validated up front.** Sampling, metrics, plot types, archiving, accuracy settings and
  fault injection all come from the YAML config. `configure()` (and so creating the framework) checks every
  section the framework uses: sampling, ammeters, metrics, chart types, accuracy settings, `enabled` switches
  (real booleans: the string `"false"` would otherwise count as on) and, when enabled, the fault set. A typo fails
  in milliseconds instead of after a full run; `tools/spec_audit.py` checks 7 kinds of invalid setting this way.
- **Tested.** A pytest suite of 26 tests (15 unit, 9 integration, 2 acceptance) covers every module, the original bugs, and the statistical claims
  in this document (see [tests and the Allure report](engineering.md#tests-and-the-allure-report)).
- **Extension points are registries.** New metrics go in `METRICS` (`result_analysis.py`), new per-run charts
  in `PLOT_TYPES` (`visualization.py`), new faults in `DEFAULT_FAULTS` (`error_simulation.py`), and new ammeters
  in the config plus an `AmmeterEmulatorBase` subclass. Config validation picks them up automatically.
- **Minimal dependencies.** Statistics, bootstrap intervals, archiving and fault injection use only the Python
  standard library. The only third-party libraries used are PyYAML (config) and matplotlib (charts).

## Sampling

- **Absolute schedule.** Reading *i* is requested at `i / frequency` seconds after the start, not "sleep one
  interval after the previous reading", so a slow request doesn't push back every later one. Measured drift was
  about 1 ms. The run then waits until the full configured duration has passed.
- **Failures don't stop a run.** A timeout, refused connection, empty reply, unparseable reply or non-finite
  number is recorded in `errors` (with its type) and sampling continues. The success rate is part of the statistics.
- **Every request has a timeout**, so a hung device can't hang the test.
- **Each failure has its own type** in `errors` / `error_counts`: `TimeoutError`, `NoReplyError` (connection closed
  with nothing sent), `TruncatedReplyError`, `OverRangeError`, `ImplausibleReadingError`, and `ValueError` for an
  unparseable or non-finite reply, so a dropped connection and a garbled reply can be told apart.
- **Practical ceiling: about 120-145 Hz.** One TCP connection per reading costs several milliseconds on Windows.
  Measured (400 readings, emulator in the same process): 200 Hz and 1000 Hz requests achieved 141-145 Hz in one
  session and 122-125 Hz in another; at 100 Hz the rate held but readings were up to 61 ms late; at 50 Hz the
  worst lateness was about 6-18 ms. Such a run isn't refused: it completes, records the achieved rate in `timing`
  and warns that it fell behind schedule. Keep the frequency at 50 Hz or below for an exact schedule.

## Statistics

- **Sample standard deviation** (n − 1), since the readings are a sample of each device's output.
- **Coefficient of variation** (std dev ÷ mean) is added to the five required metrics. The ammeters' readings
  differ by more than 1000×, so absolute spread can't be compared across them; CV can.
- Metrics that can't be computed (no readings, std dev from one reading, CV with a zero mean) are `None`
  rather than an exception or a misleading 0.

## Result archiving

- **One JSON file per run**, `results/archive/<test_id>.json`, holding the full result plus metadata (archive
  time, host, platform, Python version, config snapshot). JSON keeps records readable and diffable without tools.
- **Unique IDs**: `<ammeter>_<8 random hex>`. A new ID is checked against the archive and re-drawn if taken, and
  files are opened in exclusive-create mode, so a record can never be overwritten.
- **Robust retrieval**: IDs are restricted to safe characters (they become file names); a corrupt file is
  skipped with a warning instead of breaking the history listing; an archiving failure is logged without losing
  the run's data.

## Visualization

- **Per run:** a time series (with mean, ±1 SD band and failed readings marked), a histogram (with mean and
  median) and a control chart. **Across runs:** a comparison chart.
- **One scale for the comparison.** Plotting the raw readings would need a separate axis per ammeter, which
  invites false comparisons. Instead each reading is divided by its run's mean, so every run is centred on 1.0 and
  its spread shows its consistency directly.
- **Colors** follow the ammeter, not its position in a chart (Greenlee blue, ENTES orange, CIRCUTOR aqua), and
  were checked with a palette validator to be distinguishable under common color-vision deficiencies. Because aqua
  is low-contrast, every chart also names the ammeters in text.
- Charts are drawn with matplotlib's `Figure` API rather than `pyplot`: no GUI window, no global state, safe to
  use from any thread or headless machine.

## Control chart

A standard QA tool for asking "is this device behaving consistently over time?". Each run's readings are checked
against three rules, and the result is stored in `result["control_chart"]`, logged as a warning when a run is out
of control, and drawn as a chart:

| Rule | Catches | How |
|---|---|---|
| Beyond limits | spikes | a reading outside mean ± 3σ, with σ from the average jump between consecutive readings (÷ 1.128) |
| Run | a shifted offset | 9 or more consecutive readings on the same side of the **median** (Nelson rule 2) |
| Trend | drift | a significant Mann-Kendall test (p < 0.01); its size is Sen's slope |

Each choice was measured against alternatives on the real emulator formulas (300 simulated runs of 40 readings
per case) rather than picked from a textbook:

- **Run rule about the median, not the mean.** A spike pulls the mean up and leaves most readings below it,
  which faked a "run" in 21% of spiky runs. The median isn't moved by a spike (3%).
- **9, not Western Electric's 8.** Fewer than half the false alarms on these skewed readings.
- **Mann-Kendall, not a least-squares slope.** It is rank-based, so two spikes near the end of a run can't fake
  a drift, which happened with a fitted line. It catches slightly fewer drifts (65% vs 72% for a 5% drift), with
  about 1% false trends on clean or spiky data.
- **Classic limits.** "Robust" limits (median ± 3σ from the median jump) looked attractive but more than
  doubled false alarms on ENTES (24% vs 10%) for no gain in spike detection.

Measured performance, 40 readings: false alarms on clean data about 4% (CIRCUTOR) and 12% (ENTES, whose skewed
readings cross the upper limit more often); 5% of readings spiked ×6 caught about 80% of the time; a 5%-per-reading
drift about 85%, a 3% drift about 50%. Greenlee's V / R readings cross the limits almost every run: they are
not a stable, symmetric process, and the chart says so.

- **Longer runs widen the rules.** Fixed rules flag more stable runs the longer a run gets, because every extra
  reading is another chance to cross a limit or extend a run. Measured on stable CIRCUTOR readings: 1 %, 6 %, 28 %
  and 98 % of runs flagged at 10, 40, 200 and 2,000 readings. So the 40-reading rules apply unchanged up to 40
  readings, and above that both widen to keep each rule's expected false alarms at its 40-reading level:
  - the limits widen by a Bonferroni correction, to 3.46 sigma at 200 readings and 4.04 at 2,000;
  - the run rule needs log2(n / 40) more readings, so runs of 12 at 200 and 15 at 2,000.

  Result: 1 %, 6 %, 6 % and 7 % flagged (ENTES: 6 %, 16 %, 6 %, 12 %). Real drift is still caught: 0.5 % per
  reading over 200 readings 99 %, 0.05 % per reading over 2,000 readings 100 %. `analysis.control_chart.sigma_limit`
  and `run_length` fix either rule if a fixed rule is preferred.

The lower limit is mean − 3σ and is kept as computed in `control_chart["lcl"]`. For skewed positive readings it
is often below 0 A, which no reading can reach, so the chart draws it at 0 A and prints the computed value beside
it rather than stretching the axis below anything measurable. Detection is unchanged: no reading is ever below
either line.

## Accuracy assessment

- **Compare like with like.** By default the assessment takes the latest run, with readings, of each ammeter in
  the config. The archive also holds calibration sweeps at other currents, failed runs and ad-hoc ammeters, and
  pooling them produced meaningless rankings (a sweep instrument with robust CV 14.8, "agreement" on an interval
  three orders of magnitude wide). The result lists the runs it used (`runs_used`); pass results explicitly to
  assess anything else.
- **Precision vs accuracy.** Accuracy means closeness to the true current, which needs a known reference. The
  emulators have none (see [the emulators](../README.md#the-ammeter-emulators)), so by default the assessment ranks
  **relative spread** (robust CV) and says so in its report. For real meters measuring one steady current that is
  their repeatability. The emulators draw a new random current per reading, so for them it measures how widely
  each formula spreads. CIRCUTOR's spread comes almost entirely from its random time step: robust CV of the time
  step alone 0.609, of its 10-voltage sum alone 0.152, of the reading 0.615 (100,000 draws). The averaging over
  10 samples contributes little. True repeatability needs a fixed measurand: that is what the instrument-model
  sweep is for. Setting `reference_current_a` adds bias and relative
  RMSE and ranks by accuracy instead. A precise but miscalibrated device ranks first without a reference and last
  with one, which is exactly the distinction this guards.
- **Bootstrap confidence intervals** (resampling the readings 2,000 times) make no assumption about the shape of
  the data, which matters for Greenlee's heavily skewed readings. A fixed seed makes reports reproducible.
  Verified: 95% intervals contained the true mean in 93.3% of 300 simulated trials, normal for this method at n = 30.
- **Robust CV** (scaled median absolute deviation ÷ median) decides the ranking, so one extreme reading can't.
  In a test, a single outlier moved the ordinary CV from 0.09 to 11.8; the robust CV stayed at 0.09.
- **Honest verdicts.** A "clear winner" is declared only when the top ammeter's interval doesn't overlap the
  runner-up's. Two non-overlapping 95 % intervals is a much stricter test than a 5 % significance test of the
  difference, so the rule errs towards "not established". Simulated through `assess_accuracy` with the emulator
  formulas (20 trials per size): a clear winner 0/20 times at 10 and at 40 readings per type, 1/20 at 200, 12/20 at
  1,000 and 20/20 at 2,000; at 10 readings the leader was the wrong type half the time. The population answer
  (200,000 draws of each formula) is CIRCUTOR robust CV 0.615 < ENTES 0.711 < Greenlee 0.808, and the shipped
  2,000-reading run (`sample_results/reliability/`) names CIRCUTOR the clear winner.
- **Small samples are flagged.** The percentile bootstrap interval of robust CV under-covers at small n: it
  contained the population value 177/200 times (88.5 %) for ENTES at 10 readings, 193-194/200 at 40. Below 30
  readings per type the assessment adds a note that the ranking is indicative only.
- **Agreement** between two ammeters is the ratio of their medians with a confidence interval; they agree if the
  interval contains 1.

## Reply framing and plausibility

- **Every reply is one line ending in a newline**, and the client reads until it. Over a real link a reply can
  arrive in pieces; the provided client made a single `recv()`, so `12.345` arriving as `12.` + `345` would have
  been stored as 12.0 A with no error (fix #13). A reply cut off before its terminator is a `TruncatedReplyError`,
  never a shorter number. A meter's over-range `OL` is an `OverRangeError`.
- **Plausibility limits.** A reading a device physically can't produce (10^9 A from a 200 A meter, a x10 spike
  beyond the formula's range) is recorded as an `ImplausibleReadingError` instead of poisoning the statistics.
  The configured limits are exactly each formula's output range, and a test keeps them in step.

## Aliasing

Mains ripple at 50 Hz sampled at 5 Hz folds to 0 Hz (a constant offset that depends on where in the cycle
sampling started); at 7 Hz it folds to a 1 Hz wander; at 49.5 Hz to 0.5 Hz, which over a short run is a steady
rise the trend test calls a drift (a test shows exactly this). Sampling above twice the mains frequency
(100-120 Hz) over a one-connection-per-reading TCP protocol isn't reliable, so instead of pretending, every run
records `aliasing` (mains, sampling and alias frequency) and warns when below the Nyquist rate, so an apparent
drift or offset can be checked against it. Every run also records `timing` (worst lateness against the schedule
and the achieved frequency) and warns when it fell behind: requested 500 Hz with 10 ms requests becomes 100 Hz,
and the result says so instead of silently stretching the run. Real instruments avoid this by
integrating over a whole number of mains cycles.

## Accuracy verification against a reference

The hardware review's main point: the formula emulators randomize what real instruments hold constant (shunt
resistance, calibration factor, sample interval), so their spread says nothing about instrument quality, and
there is no true current to check accuracy against. So, without changing their default behaviour:

- **`ReferenceSource`** holds the true current, like a calibrator (DC, or AC as RMS), with its own uncertainty.
- **`InstrumentModel`** measures it as a meter does: fixed gain error and offset, an absolute noise floor plus
  noise proportional to the current, quantization to the resolution (fixed digits, as a display shows), `OL`
  beyond range (+5 % headroom), DC blindness for a Rogowski coil (it senses dI/dt), and a burden resistance. An
  emulator given a model answers from it instead of its formula. The config describes three datasheets: a shunt
  meter (Greenlee), a Hall clamp (ENTES) and an AC-only Rogowski coil (CIRCUTOR).
- **Multi-point sweep**: 0, +/-10, 50, 90, 100 % of range (negative points only for DC), each checked against
  +/-(x % of reading + y counts).
- **GUM uncertainty** per point: Type A = std / sqrt(n), Type B from quantization = resolution / sqrt(12), expanded
  with k = 2, and the normalized error En = error / sqrt(U_meter^2 + U_reference^2). Both verdicts are reported
  because they answer different questions: *within spec* is the product acceptance test; |En| <= 1 is the
  calibration-lab question of whether the meter agrees with the reference within its own uncertainty. A meter with
  a 0.3 % gain error passes a 0.5 % spec but has En >> 1: its systematic error dwarfs its repeatability.
- **Supply sweep** (the specification's diagram): the board is off below its 2.7 V brown-out, then draws a
  regulated current; the meter in series takes I x R_burden from it. Each point reports board on/off, burden
  voltage (limit: 2 % of supply), whether the burden itself browned the board out, the reading against spec, and
  whether the current is below the meter's resolution. Tests show a 10 ohm shunt browning the board out at 3 V,
  and 25 uA of sleep current being invisible to a 1 mA-resolution meter (it needs a lower range: auto-ranging).
- The cross-type assessment (`accuracy.py`) also reports each type's Type A uncertainty and U (k = 2).

## Error simulation

- **Faults are injected inside the emulators**, so the framework meets real socket timeouts, dropped
  connections and garbled replies, as it would from real hardware, rather than mocked exceptions.
- **Three kinds of fault.** *Lost-reply* faults (no response, dropped connection, corrupted reply, truncated
  reply) each cost one reading, so injected and detected counts can be checked against each other. One caveat: the framework sends
  one request at a time and an emulator serves one connection at a time, so the request after a hang waits for
  the hang to end. If a hang lasts more than twice the request timeout, that next request times out too, without
  a fault of its own (as with a real hung device), and detected failures exceed injected faults. *Measurement* faults
  (offset, drift, noise, spikes, mains ripple) return valid numbers that are wrong; offset and drift can only be
  caught by the accuracy assessment with a reference current. *Delivery* faults arrive intact but test the client:
  latency (late but within the timeout) tests the schedule, and fragmented replies (split into two packets) test
  that the client reassembles them.
- **Off by default, seeded and per-ammeter**, so normal runs are unaffected, fault patterns are reproducible, and
  each ammeter can be given its own problem (for example a miscalibrated ENTES).
- Emulators serve one client at a time, so a simulated hang also delays the readings after it, as a hung device
  would. Runs with simulation enabled take longer than configured.
