# Finite-population confidence stopping

This is a new research policy, independent of frozen v7/v9. It assumes exact
AVCC VCL observations of units in a complete, disjoint closed-GOP partition.
It does not claim that the existing local replay is exact on every new source.
Reference equality and source/toolchain identity remain separate obligations.

## What can and cannot shrink the legal range

For a frozen source-only or earlier-pilot-fixed surrogate mu_i and a legal
byte cap U_i, residual r_i=B_i-mu_i lies in [-mu_i,U_i-mu_i]. Its width is
still U_i. Adding a control variate cannot turn a sample residual maximum into
a deterministic error bound. A logarithm or bounded transform needs a valid
inverse-tail bound to recover the total byte target; Jensen's inequality does
not identify the arithmetic total from a log mean. Source stratification can
reduce maximum caps and observed residual variation within a stratum, but
using observed maxima as support limits would change the claim.

The known-pilot total and fixed mu are legitimate for a later random audit.
The probability statement is conditional on that complete pilot transcript;
there is no exchangeability or learned calibration assumption across sources.

## Exact nonnegative betting sequence

Let T be a candidate total of all audit-population bytes, S the exactly known
sum, R the current unknown units, n=|R|, and M=sum_R mu_i. A random next unit
must be uniform in R. Its residual Y has conditional mean

```
E[Y | past] = (T-S-M)/n              under the true total T.
a = min_R(-mu_i), b = max_R(U_i-mu_i)
x = (Y-a)/(b-a)
m(T) = ((T-S-M)/n-a)/(b-a)
```

For every candidate still consistent with deterministic per-unit bounds,
x and m lie in [0,1]. Two predictable factors have mean one and are
nonnegative:

```
upper factor = (1-x)/(1-m(T))
lower factor = 1/2 + x/(2*m(T))
```

At the degenerate true-null endpoints m=x=0 or m=x=1, define the respective
factor as one. Inconsistent candidates are excluded by deterministic bounds.
Each product is a nonnegative martingale under its own candidate T. Ville's
inequality excludes candidates whose product ever reaches 1/alpha_side.
Allocate alpha_upper=0.8alpha and alpha_lower=0.2alpha. The intersection of
both inverted tests and deterministic bounds contains the true total at
**every** observation and stopping time with probability at least 1-alpha.
The confidence kernel uses exact integers and Fraction arithmetic, with no
floating threshold decision. Monotonic factors permit integer binary-search
inversion; a past threshold crossing remains excluded if later wealth drops.

The upper arm is a risk-limiting bounded-mean test. Its finite-population
conditional mean and declining remaining range give it a stronger endpoint
behaviour than treating observations as IID. It does not escape the penalty
for a possible unobserved giant byte contribution. The lower arm is less
aggressive than an all-in x/m factor so a zero observation does not erase all
subsequent lower-bound evidence.

## Adaptive census, strata and cost

An exact census disclosure of any unit updates S and R without multiplying
either wealth. Its selection may depend on prior audit results. Future random
draws must remain uniform over the updated R, so the mean-one argument still
holds. Census permanently removes that unit's uncertainty, unlike changing
its proxy. A cost-constrained policy can prioritize remaining uncertainty per
encode cost for census and preserve its confidence statement.

For source-frozen strata, maintain separate sequences and split alpha across
the occupied strata before auditing. Adaptively choosing which stratum to
visit is legal; independent sampling across strata is not required for the
union bound. Adding many small strata spends risk without adding observations.
The first implementation uses one stratum; source quartile stratification is
a declared control only when every stratum has at least eight unknown units.

Uniform sampling and unequal costs require care. Reserve the next uniform
draw **before** admission. If that unit exceeds the remaining budget, stop the
random arm; selecting a cheaper replacement and continuing the same uniform
confidence sequence is invalid. Exact census can continue if affordable.
The budget counts the entire provided GOP plus required future input, not
just unknown or centrally scored frames. Pilot duplicates, lookahead, decode,
cache lookup, source identity checks, plan preparation and writing are distinct
costs, not free sampling. A blocked audit returns an unsatisfied interval.

The research kernel freezes caps/proxies and separates random observations
from exact census. `reserve_uniform` plus a budget rejection guard prevents a
caller from silently resampling an affordable unit. A real implementation may
use `secrets.randbelow`; fixed-seed PRNG trials are development emulation under
the declared ideal-uniform random-bit model, not adversary-safe guarantees.

## Relative-error certificate

For a nonnegative interval [L,U] with positive L+U, the harmonic point
2LU/(L+U) minimizes its largest relative error. The certified radius is
(U-L)/(U+L). Stop as satisfied only when this exact radius is at most the
requested tolerance. A model's own point remains separate. If L=0<U, the
radius is one and a small relative-error request cannot pass. Exact all-zero
census has radius zero. An empty statistical set cannot satisfy a request.
Only exactly known non-VCL bytes can be added without widening this proof's
scope; an approximate container point is not an exact offset.

## Sharp hidden-unit diagnostic

Consider two capped finite populations: every unit is zero, or one unknown
unit j contains H>0. Their transcripts coincide whenever j is not observed.
If a deterministic interval on that all-zero transcript excludes H, its
noncoverage probability under the second population is at least 1-pi_j.
For coverage 1-alpha it needs pi_j>=1-alpha, or it must retain H in the
interval. Unequal-cost pi_j=0 is decisive. This construction is a legal
bounded-population inference counterexample; claiming it represents a
specific x264 source requires a separate real encoder construction.

For n uniform draws from N equal caps, no proxies, and zero observations,
the upper wealth at T=U is exactly N/(N-n). It reaches 1/alpha_upper only
at n/N>=1-alpha_upper. Thus the kernel reflects the hidden-unit obstruction
rather than issuing a spurious low-variance certificate. Existing physically
real low-resolution alias/hidden-burst encoder counterexamples motivate the
same concern, but are not substituted for this probability calculation.

## Registration and acceptance

Freeze this derivation, the kernel, oracle adapter, source partitions/caps,
pilot inputs, allowed risk/tolerance/budget settings, seeds and dataset scope
before running the new development emulation. Existing nine-source full
references are already opened development labels. Their later replay here
does not become prospective evidence. No new NASA/TDF media or labels are
used, and frozen v7/v9 files are untouched.

Mechanical checks must independently enumerate random permutations and
confirm time-uniform coverage, cap violations, census conditioning, endpoint
handling, budget skipping refusal, monotonic inversion and relative radius.
Report every unsatisfied/overbudget case. Simulated entropy accounting is not
a new actual runtime measurement, and reading old complete references for
evaluation must be listed outside the inference interface. The current loose
CABAC cap may force census; success criteria include honest refusal and an
implementable reliable fallback, not an invented certificate.
