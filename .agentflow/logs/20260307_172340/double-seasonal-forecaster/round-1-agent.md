The file has been created at `HealthPulse/ML/TimeSeriesForecaster.swift`. Here's what was implemented:

### `HoltWintersState`
- **`weekSeasonal`** — period 7 (weekly patterns)
- **`monthSeasonal`** — period = `monthPeriod` (defaults to 28, or computed from menstrual cycle data)
- **`quarterlySeasonal`** — optional, period 90, activated when data exceeds 180 days

### `Smoothing` parameters
- `alpha` (level), `beta` (trend), `gammaWeekly`, `gammaMonthly`, `gammaSeasonal` (quarterly)

### `fit(data:monthPeriod:)`
- Grid search over all 5 smoothing parameters
- `gammaMonthly` searched in `[0.05, 0.1, 0.2]` as specified
- `gammaSeasonal` (quarterly) also searched in `[0.05, 0.1, 0.2]` when data > 180 days

### `fitWithParams()` — core fitting logic
1. Initializes level, trend, and all seasonal arrays from the data
2. For each time step: computes one-step-ahead prediction, updates level and trend
3. Updates weekly seasonal factor first (existing logic)
4. Then updates monthly seasonal factor on the weekly-deseasonalized residuals
5. Then updates quarterly seasonal factor (if enabled) on the doubly-deseasonalized residuals

### `forecast(state:smoothing:horizon:)`
- `prediction = (level + trend * h) * weekSeasonal[h % 7] * monthSeasonal[h % monthPeriod]`
- Multiplied by `quarterlySeasonal[h % 90]` when the third component is present

### HealthKit menstrual cycle integration
- `fetchMenstrualCyclePeriod()` queries `HKCategoryTypeIdentifier.menstrualFlow`
- Detects cycle start dates via 3+ day gaps in flow samples
- Computes average cycle length from the last 6 cycles
- Returns the average (clamped to 21–45 days) or defaults to 28
