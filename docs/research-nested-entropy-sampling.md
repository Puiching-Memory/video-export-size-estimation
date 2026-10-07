# Sparse two-level entropy sampling under a hard budget

This SOURCE-only proposal removes the requirement to encode a full cheap
auxiliary census. It is standard two-phase finite-population sampling applied
to export VCL totals, rather than a new statistical theorem. Earlier two-draw,
antithetic and cross-fit proposals change the exact-panel law or its fitting;
they still require the full auxiliary total. The forced-plan cheap auxiliary
also retains100% entropy. Here only a wider random set of units gets cheap
entropy, and a nested smaller set gets exact medium entropy. No actual proxy,
target value, score point or media is read; no encoder or compiler is invoked.
The96KiB family includes the canonical code/brief and reserves16KiB for final
accounting, with a128MiB disk floor. This brief is not an industrial success.

The fixed population and estimator
----------------------------------

Partition unknown target frames into disjoint units before new audit coins.
Closed SOURCE GOPs are a simple first choice; a known pilot can remove known
frames from the unit sum without removing their encoding cost. Let K be the
already paid, exact known-byte sum; let Y_i be the continuous original medium
export's VCL bytes for the unknown frames in unit i. Let mu0_i be a SOURCE-only
prediction, such as the old frozen SATD/AQ/Haar typed prediction conditional on
already paid known pilots. A missing predictor may be zero. Do not fit it to
new F/M values. Compute its full population sum from SOURCE metadata.

Let a_i be a fixed cheap potential value, defined for EVERY unit even when it
will not be computed. It can be biased and need not reproduce continuous
medium state. Define r_i=a_i−mu0_i and s_i=Y_i−a_i. Draw a registered joint law
of F and M with M contained in F. Compute cheap values only for F, and exact
values only for M. With unconditional unknown-unit marginals p_i and q_i:

`H = K + sum_U mu0_i + sum_(i in F) r_i/p_i + sum_(i in M) s_i/q_i`.

Conditional on SOURCE and prior known values, all potentials and the law are
fixed. Linearity gives E[H]=K+sum_U Y_i. Independence between layers, between
GOPs, or between frame residuals is unnecessary. Positive p_i and q_i are
required for every unknown unit; conditional q_i given the selected F is NOT
the denominator. The same exact a_i must appear in both terms. Signed output
must be retained; a negative clamp or successful-only issuance breaks this
unconditional expectation. No confidence interval follows from this proof.

The general variance retains all dependencies. Write PFF_ij=P(i,j in F),
PMM_ij=P(i,j in M), and PFM_ij=P(i in F,j in M). Then

`V = sum_ij [(PFF_ij/(p_i p_j)−1)r_i r_j +
            (PMM_ij/(q_i q_j)−1)s_i s_j +
          2(PFM_ij/(p_i q_j)−1)r_i s_j]`.

Even same-unit PFM_ii=q_i, rather than p_i*q_i. Arbitrary feasible-panel
libraries may be enumerated or sampled with exact integer tickets, and all
three joint matrices/marginals must be derived from THAT joint law. Joining
separately designed F/M panels after a draw does not preserve this law.

One practical hard-budget law and allocation
-------------------------------------------

Use disjoint SOURCE strata, containing H_h comparable units. Freeze each
unit's cheap input closure, exact global-state closure, clocks and full cost
upper bounds. Charge a common stratum upper cost cF_h for one complete cheap
unit and cM_h for the exact branch, including duplicate context/references,
rounded frames, hashes, imports and process startup. cM_h is additional to the
already paid cheap unit. Include SOURCE, known pilot and any shared decode
bill in a fixed bill C0; SOURCE is never free. Raw-frame input, CPU, wall,
memory and I/O are separate contracts, rather than interchangeable units.

Choose integers1≤m_h≤f_h≤H_h before new labels, with
`C0 + sum_h(cF_h f_h+cM_h m_h) ≤ B`.
An explicitly registered f=m branch cancels a algebraically and omits cheap
encoding altogether, so its upper charge is cM_h*m_h. The pure allocation
includes this SOURCE-only control; it never pays for a value that cancels.
Choose uniformly f_h units without replacement, then uniformly m_h from that
F set. Different stratum draws are independent. Every actual panel obeys the
same conservative charge bound; this is stronger than an expected-budget
Bernoulli design. If variable reference closures dominate, separate strata by
their upper closure costs or use a separately frozen feasible joint library.
Do not drop an expensive drawn unit, retry until affordable or pretend repeated
windows cost only their union. Any union discount needs a measured shared
decoder executor and its own preflight bound, before the law is frozen.

Within one stratum p=f/H, q=m/H; distinct-unit PFF=f(f−1)/(H(H−1)),
PMM=m(m−1)/(H(H−1)), PFM=m(f−1)/(H(H−1)); same-unit PFM=m/H.
For sample population variances/covariance with denominator H−1,

`V_h = H²[(1/f−1/H)(S_r²+2S_rs)+(1/m−1/H)S_s²]`.

Write A=S_r²+2S_rs, D=S_s². Where A,D are positive and caps/nesting inactive,
a continuous Lagrange allocation satisfies f_h proportional to
`H_h*sqrt(A_h/cF_h)`, m_h proportional to `H_h*sqrt(D_h/cM_h)`.
If this requests f<m, the nesting boundary f=m has combined coefficient
A+D=S_(Y−mu0)² and cost cM when the cancelled cheap branch is omitted.
Caps, integer costs and support floors require
discrete allocation. If A≤0, more cheap observations at fixed m can increase
variance, and the conditional optimum is f=m; there is no universal promise
that widening F helps. A census F=U reduces to the old full-a control variate,
while F=M algebraically cancels a and reduces to ordinary mu0 control variates.

Actual Y is unavailable at allocation time. Do not use oracle residual
variances as deployable weights. One robust planning objective uses SOURCE or
external-training bounds |r_i|≤R_h, |s_i|≤S_h. For H>1 set
`Abar=H/(H−1)*(R²+2RS)`, `Dbar=H/(H−1)*S²`.
These upper-bound the variance expression after finite population corrections;
the constant terms do not affect the allocation objective
`sum_h H_h²(Abar_h/f_h+Dbar_h/m_h)`.
If R,S are merely SOURCE heuristics, call this a planning proxy, not a certified
variance/coverage bound. Existing broad CABAC bounds may be valid but too loose
to make a useful allocation. A small, independently paid external calibration
can inform planning, but must not spend fresh F/M Y to alter the registered law.

The pure runner implements exact multiple-choice knapsack on invented small
strata, compares it to exhaustive products, and rejects impossible allocations.
For large populations, freeze a geometric count grid1,2,4,...,H, include f=m
and census choices, remove cost/objective dominated options and run a bounded
integer-budget DP. This optimizes over that specified grid, rather than all
feasible panel laws or unknown target variance. Enforce an explicit minimum
q floor when reliability requires bounded inverse weights; refuse if it cannot
fit. A requested finer grid/changed floor creates a new SOURCE registration.

Minimal implementable cheap-unit protocol
----------------------------------------

The lowest-risk first oracle is an INDEPENDENT local cheap encode per fixed
unit, using an ordinary pinned single-thread veryfast CRF23 backend, fixed
prepared I420 source pixels and fixed context. For a GOP [d0,d1), freeze raw
input [max(0,d0−48),min(N,d1+48)) once from SOURCE, signal REAL local segment
EOF at its defined end, and count only VCL NALs types1..5 and four-byte lengths
whose original display ID belongs to this unit's unknown-frame set. Leading
reset/IDR and local lookahead differ from continuous export, which is allowed
for a. Geometry/SAR/VUI/timebase/filter/thread/CPU flags, all x264 parameters,
timestamp mapping, the original source-media digest and raw-window digest are
part of the unit key. Context48 here is a fixed recipe, not a claim that it
recovers continuous state. Never reuse such local output as exact medium Y.

Each unit executes in a fresh process, with a fresh DPB/RC owner, and its
window/start/reset cannot depend on F, M, other selected units or execution
order. F and M share the ONE cached a_i under this complete key. Encode no
cheap units outside F. Persist per-unit scalar, counted frame IDs, normalized
parameter/header keys, complete raw/hash/input/CPU receipts and EOF/cleanup.
If the cheap branch fails, poison the whole draw and retain its spent work;
successful-unit-only HT is invalid. Before real scoring, run two arbitrary
selection contexts and both execution orders against identical single-unit
inputs; assert nonempty byte equality and identical a_i. This is a fixed
potential test, not an accuracy or continuous-state equality test.

`src/vsize/context_probe.py` already describes padded local windows, and the
ordinary full-proxy FFmpeg command can encode a deterministic finite segment.
Neither exposes this exact fixed-unit value/key/caching/finite-law executor.
A new thin broker must implement it. A SOURCE-QPC imported cheap oracle is
another possible fixed potential, but requires deterministic per-unit reset
and fixed reference closure. Existing full forced-plan AUX streaming has one
stateful DPB; skipping cheap GOPs changes later a unless its per-unit closure
is fixed independently. Do not silently use a selected-union streaming DPB.

Exact M remains the hard mechanical boundary
------------------------------------------

Y_i must equal the original continuous target's bytes for this unit, with
global SOURCE/QPC/state, exact SPS/PPS/clock identity and real decoded pixel
checks. A medium local warm clip is generally a different Y and cannot be
substituted into the proof. A fixed medium prefix from a true closed reset,
through every target unit frame and all required references, may supply a
unit oracle if the global-state replay is independently proved. All those
inputs, duplicate prefixes and lookahead are billed even if only the unit's
unknown frames enter H. Full GOP or earliest safe prefix cost can exceed a
20/50% quota. A library with zero q for any residual unit must refuse.

Current virtual queue is a restricted single-owner medium-only profile,
requiring genuine SOURCE EOF and N≤896, imports/AQ/lowres setup and a reference
complete prefix. The current frontend/provider has exactly the motion B8 raw
IDs. ABI2 fixes reader shape but its SOURCE-only proofs do not yet establish
actual native byte/clock/pixel equality or a generic per-GOP API. It cannot
currently service arbitrary M or cheap local profiles. Disposable-B omission
alone does not remove the mandatory reference floor. Until exact M unit
oracles are proved, this estimator is an executable rational design and an
implementable cheap-oracle recipe, not a runnable production export estimate.

Registered pure evidence and explicit failure boundaries
-------------------------------------------------------

The one pure run exhausts invented signed r/s vectors over four nested laws,
including a nonuniform, nonfactorial joint law. It verifies exact expectation,
the complete joint-covariance formula and the uniform finite-population
formula; verifies cap envelopes and allocation; and checks support/nesting/
budget refusal. A counterexample changes the shared a_1 according to F and
produces mean80 for true30, despite sharing the same a in both terms. Another
has r=−s: F=M is exact, while widening F with fixed M introduces variance.
No actual bytes, variance gain, total-cost reduction, CI or SOTA result follows.

Reject before coins if unknown-unit coverage, positive marginals, original
continuous-target identity, fixed unit closures, resource upper bounds, genuine
SOURCE EOF or minimum q floor is missing. Reject/poison rather than change law
after a native failure, cap overflow, recipe mismatch or partial unit. Do not
refit a beta on fresh F/M, clamp signs, merge stateful proxy sessions or choose
a cheap preset after target scoring. Freeze cost/profile/model/law/prediction
before any independent full target truth is opened. Keep existing full-a HT
and SOURCE-only HT as separately charged controls. The intended benefit is
spending entropy only where its marginal variance reduction pays; whether
that beats a complete export is an empirical cloud test still to perform.
