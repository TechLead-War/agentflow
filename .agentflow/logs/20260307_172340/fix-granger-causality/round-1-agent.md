The file compiles cleanly — only deprecation warnings for the legacy CBLAS/LAPACK API (resolved at the project level with `-DACCELERATE_NEW_LAPACK`, not in source).

Here's what the implementation provides:

**`CorrelationDiscovery.swift`** — Granger causality with proper OLS and F-test:

1. **`grangerCausalityTest(x:y:maxLag:)`** — Builds two design matrices:
   - **Restricted model**: intercept + y lags only
   - **Unrestricted model**: intercept + y lags + x lags
   - Computes F-statistic from SSE reduction

2. **`solveOLS(designMatrix:n:p:response:)`** — Proper OLS via normal equations:
   - Computes X'X using `cblas_dgemm` and X'y using `cblas_dgemv`
   - Solves X'X·β = X'y with Accelerate's `dgesv_` (LU factorization)

3. **`fDistributionSurvival(f:d1:d2:)`** — P(F > f) using the identity:
   - P(F > f) = I_x(d₂/2, d₁/2) where x = d₂/(d₂ + d₁·f)

4. **`regularizedIncompleteBeta(x:a:b:)`** — Full implementation with:
   - Symmetry relation for convergence when x > (a+1)/(a+b+2)
   - `betaContinuedFraction()` using Lentz's algorithm (200 iterations, 1e-14 tolerance)
   - Log-space prefactor computation for numerical stability
   - `lnBeta()` via `lgamma` for the Beta function
