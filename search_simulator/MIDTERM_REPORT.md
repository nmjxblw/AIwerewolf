# Midterm Report: An Exact-Belief Payoff Matrix for Cheap Talk in the Werewolf Game

## Executive Summary

This report presents the team's exact-belief, model-conditioned payoff matrix for the Werewolf game. The study examines how cheap talk—strategically relevant but non-binding public communication—affects player beliefs, subsequent voting, and terminal zero-sum utility. The longer-term objectives are to compare ex ante estimates with repeated-game outcomes and to provide language-model agents with an interpretable decision instrument. As no real-game dataset is currently available, the scope of this report is limited to the mathematical design, implemented evaluation pipeline, and preliminary simulation outputs.

Complete future-game-tree enumeration is replaced by a two-layer formulation. The belief layer performs exact conditioning over legal hidden-role assignments. The decision layer samples from the resulting posterior and conducts forward terminal Monte Carlo simulation under an explicit utility-ranked policy. Each concrete speech action is evaluated at three evidence-strength settings. The reported outputs comprise terminal utility, numerical uncertainty, the paired difference from a neutral reference action termed the matrix baseline, and explanatory reaction counts. All reported observations are conditional on the specified model; they neither establish equilibrium properties nor constitute validated predictions of human or language-model behaviour.

## Situation

The Werewolf game combines private roles, asymmetric information, public communication, and delayed consequences. Non-binding statements may nevertheless alter beliefs, votes, and terminal utility. The research problem is therefore formulated as the ex ante evaluation of alternative speech actions under the information legally available to a specific role.

The study builds on Shitong Wang's *Optimal Strategy in the Werewolf Game: A Theoretical Study*. That work establishes a foundation based on zero-sum winning probabilities, "random strategy+", incomplete-information modelling, and a recursive choice between concealing and revealing verified information under an honesty constraint. It identifies dishonest communication, sequential speech, and additional roles as open research directions. The present project operationally extends this framework to structured truthful and deceptive speech by multiple roles in sequence. The equilibrium results of the foundation paper are not assumed to hold under the extended setting.

Full future-tree expansion is unsuitable for the primary estimator because hidden assignments, speech targets, votes, role abilities, ties, and subsequent reactions generate combinatorial growth. In addition, structural path counts do not represent behavioural probabilities. Repeated language-model games incur substantial computational cost, remain sensitive to prompting, and may reproduce a restricted set of historical action preferences. These limitations motivate a controlled action-level estimator in place of exhaustive path counting or empirical imitation of a single model.

The current study adopts a fixed seven-player role configuration comprising two Werewolves, two Villagers, one Witch, one Seer, and one Guard, which yields 1,260 legal hidden-role assignments. The evaluated first-day speech actions include false Seer claims by Villagers or Werewolves, Seer silence, accusations, support, voting intentions, and a neutral reference action. This finite setting provides explicit and auditable information and action boundaries.

## Task

The team's midterm task was to construct a payoff matrix for estimating expected terminal utility conditional on the public state, the actor's private role knowledge, the exact posterior over legal hidden worlds, a concrete speech intervention, and a fixed future-play model. Each matrix cell was required to have stable semantics, a matched comparison with the matrix baseline, an uncertainty estimate, and reproducibility across process counts and execution orders. All equally ranked targets were required to remain explicit, while omniscient observer data were excluded from role-scoped decision queries.

The present-stage outputs consist of the ex ante matrix and preliminary simulation evidence. Subsequent empirical work will record real games through the API and compare predicted values with repeated outcomes. In the absence of this dataset, no claim of fitting to or validation against real-game outcomes is made.

## Action

### Matrix structure and output contract

The matrix is indexed by actor, concrete action, and evidence strength. Rows are complete second-level speech actions rather than broad tactic labels. Columns are the three evidence settings. A cell is the following tuple:

$$
\mathcal M^{(i)}_{a,c}
=
\left(
\widehat V_N^{(i)}(a,c),
\operatorname{SE}_N(a,c),
\widehat\Delta_N^{(i)}(a,c),
\operatorname{SE}_{\Delta,N}(a,c),
N,
\mathbf n_{\mathrm{scenario}}
\right).
$$

| Symbol | Definition |
| --- | --- |
| `\mathcal M^{(i)}_{a,c}` | Matrix cell for actor `i`, concrete action `a`, and evidence strength `c` |
| `i` | Index of the acting player whose lawful information and camp utility define the evaluation perspective |
| `a` | Concrete structured speech action represented by one matrix row |
| `c` | Evidence-strength parameter represented by one matrix column; the evaluated levels are 0, 0.5, and 0.8 |
| `\widehat V_N^{(i)}(a,c)` | Monte Carlo estimate of actor `i`'s expected terminal utility for action `a` at evidence strength `c` |
| `\operatorname{SE}_N(a,c)` | Monte Carlo standard error of the estimated terminal utility |
| `\widehat\Delta_N^{(i)}(a,c)` | Estimated mean paired utility difference between action `a` and the matrix baseline |
| `\operatorname{SE}_{\Delta,N}(a,c)` | Standard error of the estimated paired utility difference |
| `N` | Number of terminal trajectories simulated for the cell; the production specification sets `N` to 100 |
| `\mathbf n_{\mathrm{scenario}}` | Six-component vector containing the mutually exclusive reaction-scenario counts |

The output is therefore a multidimensional statistical summary rather than an aggregated strategy score. It reports estimated terminal utility, Monte Carlo standard error, the paired difference from the matrix baseline and its standard error, sample count, and six reaction-scenario counts. The three evidence-strength columns are retained separately. First-level action families serve only as navigation summaries and are not averaged into a composite value.

### Operational definition of the baseline

The matrix baseline is operationally defined as the actor's scheduled speaking-turn event in the absence of any evaluated tactical intervention. It contains no accusation target, support target, intended vote, role claim, claimed inspection result, or tactic label, and its communication intensity is zero. The event advances the normal speaking sequence but does not encode a natural-language utterance. It remains distinct from explicit silence as an action identity.

| Term | Operational meaning | Purpose |
| --- | --- | --- |
| Matrix baseline | Neutral structured speaking-turn event without tactical content | Reference action for estimating each candidate's paired utility difference |
| Explicit silence | Separate candidate representing the deliberate omission of public content | Evaluation of silence as an independent strategic action |
| Experimental baseline | Future full-game control condition without the Villager decoy-Seer or Werewolf counterclaim treatments; the true Seer remains required to provide an effective statement | Estimation of treatment effects across repeated real games |

The neutral baseline and explicit silence may produce identical numerical estimates under the current abstraction. Their separation preserves distinct experimental semantics: the former represents the absence of the evaluated tactical intervention, whereas the latter is an explicit strategic action included in the candidate ranking.

Matched comparison requires each candidate and its matrix baseline to share the same public state, actor information, hidden-world sample, and future random stream. The paired difference consequently estimates the change in terminal utility attributable to replacing the neutral reference action with the candidate under the specified model. The baseline has no universal win-rate interpretation because its value is conditional on the actor, role view, game state, evidence-strength column, and future-play model. Accordingly, a baseline estimate of -0.4 in the results table applies only to the associated row and experimental condition.

### Separating exact belief from future simulation

The methodological redesign distinguishes present uncertainty from future combinatorial growth. For the fixed board, the belief layer enumerates all 1,260 legal role assignments, removes worlds inconsistent with the actor's information, and normalises the remainder. Given the previous belief and the likelihood of new evidence, the actor-specific posterior is

$$
B_t^{(i)}(\theta)
=
\frac{B_{t-1}^{(i)}(\theta)L_t^{(i)}(\theta)}
{\sum_{\theta'}B_{t-1}^{(i)}(\theta')L_t^{(i)}(\theta')}.
$$

| Symbol | Definition |
| --- | --- |
| `B_t^{(i)}(\theta)` | Posterior probability assigned by actor `i` at decision time `t` to hidden-role assignment `\theta` |
| `B_{t-1}^{(i)}(\theta)` | Actor-specific belief in assignment `\theta` before incorporating the current evidence |
| `L_t^{(i)}(\theta)` | Likelihood of the newly observed evidence under assignment `\theta`, evaluated from actor `i`'s information perspective |
| `\theta` | One legal complete hidden-role assignment in the finite state space |
| `\theta'` | Summation index over all legal assignments retained by the actor's information constraints |
| `t` | Sequential decision or evidence-update index |

It is neither Monte Carlo approximated nor top-k truncated. The value of a concrete speech action is then defined over a hidden world drawn from this complete posterior and a terminal trajectory generated by the fixed rollout policy:

$$
V^{(i)}(a,c)
=
\mathbb E_{\Theta\sim B_t^{(i)},\;\tau\sim P_{\rho,c}(\cdot\mid do(a),\Theta,I_t^{(i)})}
\left[u_i(\tau)\right].
$$

| Symbol | Definition |
| --- | --- |
| `V^{(i)}(a,c)` | Theoretical model-conditioned expected terminal utility for actor `i`; this is the estimand approximated by the simulation mean |
| `\mathbb E` | Expectation over both hidden-world uncertainty and stochastic future trajectories |
| `\Theta` | Random hidden-role assignment sampled from actor `i`'s exact posterior |
| `\tau` | Random trajectory from the current intervention to a terminal game state |
| `P_{\rho,c}` | Distribution over future trajectories induced by policy `\rho`, evidence strength `c`, and the explicit rule randomness |
| `\rho` | Fixed utility-ranked policy governing simulated future actions |
| `do(a)` | Intervention that replaces the actor's current speech event with concrete action `a` while holding the initial condition fixed |
| `I_t^{(i)}` | Public and private information legally available to actor `i` at decision time `t` |
| `u_i(\tau)` | Terminal camp utility on trajectory `\tau`: +1 for victory of actor `i`'s camp and -1 for defeat |

This is an ex ante, model-conditioned value from the actor's information perspective.

Each actor receives a separate role view. Every player knows their own exact role. Werewolves know their teammate, while the Seer knows only the camp results of completed checks rather than the exact good-role subtype. Public declarations, deaths without role revelation, suspicions, and high posterior ranks do not become hard identity facts. Complete hidden assignments are available to the research observer for audit, but they are rejected as inputs to the decision matrix. This creates two conceptual API channels: an observer channel for subsequent evaluation and a role-scoped channel for action computation.

### Converting speech into comparable actions

Natural-language utterances were replaced with structured speech plans. The current first-level families are the neutral baseline, accusation, support, voting intention, Seer claim, and explicit silence. They are navigation categories rather than the actual units of evaluation. Each second-level action retains the strategically relevant fields: target seat, claimed identity, claimed check target and camp result, communication intensity, and tactic label. A complete false Seer claim must name another living player and report that player as good or Werewolf. A weak identity claim without a check is represented separately. A real Seer may reveal only an actual private check, while Seer silence is an explicit empty speech event rather than an error or placeholder sentence.

This abstraction deliberately discards wording, style, and prompt text. It preserves the semantic factors that can change beliefs and later votes. It also prevents two substantively different claims from being collapsed merely because they share the label "claim Seer". All equally ranked targets remain in the matrix; seat order is never used as an arbitrary tie-breaker.

### Modelling evidence and future behaviour

The current evidence-strength settings are 0, 0.5, and 0.8. They are sensitivity anchors, not probabilities that a statement is true. Speech likelihood is interpolated with a neutral likelihood:

$$
L_c(e\mid\theta)
=
(1-c)+cL_{\mathrm{speech}}(e\mid\theta),
\qquad
c\in\{0,0.5,0.8\}.
$$

| Symbol | Definition |
| --- | --- |
| `e` | Structured speech-evidence event being incorporated into the belief update |
| `L_{\mathrm{speech}}(e\mid\theta)` | Base likelihood assigned to evidence `e` under hidden assignment `\theta` |
| `L_c(e\mid\theta)` | Evidence likelihood after interpolation at strength `c` |
| `c` | Interpolation strength in the interval from 0 to 1; this study evaluates 0, 0.5, and 0.8 |
| `1-c` | Weight assigned to the neutral unit likelihood; at `c=0`, the speech event contributes no identity evidence |
| `\theta` | Candidate hidden-role assignment under evaluation |

Thus zero ignores the speech as identity evidence, while the other settings represent moderate and stronger effects. They remain separate and may later be calibrated by real-game or language-model belief updates.

Future simulated players do not call a language model or recursively construct another matrix. An explicit utility-ranked policy applies hard rules, tactic constraints, camp objectives, role priorities, and equal treatment of tied actions. Five scores - 1, 0.75, 0.5, 0.25, and 0 - generate probabilities through

$$
\pi_{\rho}(a\mid s,i)
=
\frac{\exp\left(Q_i(s,a)/\tau_{\mathrm{policy}}\right)}
{\sum_{a'\in\mathcal A_i(s)}\exp\left(Q_i(s,a')/\tau_{\mathrm{policy}}\right)},
\qquad
\tau_{\mathrm{policy}}=0.25.
$$

| Symbol | Definition |
| --- | --- |
| `\pi_{\rho}(a\mid s,i)` | Probability that policy `\rho` selects action `a` for actor `i` in state `s` |
| `s` | Current simulated decision state |
| `\mathcal A_i(s)` | Set of legal actions available to actor `i` in state `s` |
| `a'` | Summation index over the legal action set |
| `Q_i(s,a)` | Normalised utility-rank score assigned to action `a`; the permitted scores are 1, 0.75, 0.5, 0.25, and 0 |
| `\tau_{\mathrm{policy}}` | Softmax temperature controlling dispersion across ranked actions; it is fixed at 0.25 and is independent of evidence strength |
| `\exp` | Exponential function used to convert scores into positive, normalised action weights |

This transparent behavioural assumption is neither historically fitted nor presented as an equilibrium or model of actual human play.

### Estimating terminal utility fairly

For every concrete action and evidence-strength setting, the production specification runs 100 trajectories to a terminal outcome. Good actors receive utility +1 for a good-team victory and -1 for a Werewolf victory; Werewolf actors use the opposite sign. The estimated value and Monte Carlo standard error are

$$
\widehat V_N^{(i)}(a,c)
=
\frac{1}{N}\sum_{n=1}^{N}u_i\left(\tau_n^{a,c}\right),
\qquad
\operatorname{SE}_N(a,c)=\frac{s_N(a,c)}{\sqrt N}.
$$

| Symbol | Definition |
| --- | --- |
| `n` | Trajectory index from 1 through `N` |
| `\tau_n^{a,c}` | Terminal trajectory generated for action `a`, evidence strength `c`, and sample index `n` |
| `u_i(\tau_n^{a,c})` | Realised terminal utility for actor `i` on that trajectory |
| `\widehat V_N^{(i)}(a,c)` | Arithmetic mean of the `N` realised terminal utilities; it estimates the theoretical value `V^{(i)}(a,c)` |
| `s_N(a,c)` | Sample standard deviation of the `N` realised utilities |
| `\operatorname{SE}_N(a,c)` | Standard error due to finite Monte Carlo sampling, calculated as the sample standard deviation divided by the square root of `N` |

Candidate actions are compared with the matrix baseline using common random numbers. The same evidence setting and sample index reuse the hidden world, future actions, tie-breaking, and rule randomness. The reference action in the second term below is the matrix baseline defined above. The paired difference is

$$
\widehat\Delta_N^{(i)}(a,c)
=
\frac{1}{N}\sum_{n=1}^{N}
\left[u_i\left(\tau_n^{a,c}\right)-u_i\left(\tau_n^{a_0,c}\right)\right].
$$

| Symbol | Definition |
| --- | --- |
| `\widehat\Delta_N^{(i)}(a,c)` | Mean of the `N` within-sample utility differences between candidate action `a` and the matrix baseline |
| `a_0` | Matrix-baseline action defined in the preceding subsection |
| `\tau_n^{a_0,c}` | Baseline trajectory generated with the same evidence strength, sample index, hidden world, and random streams as the candidate trajectory |
| `N` | Number of paired candidate-baseline trajectory comparisons |

Writing each paired outcome difference as a sample value, its standard error is

$$
d_n(a,c)
=
u_i\left(\tau_n^{a,c}\right)-u_i\left(\tau_n^{a_0,c}\right),
\qquad
\operatorname{SE}_{\Delta,N}(a,c)
=
\frac{1}{\sqrt N}
\sqrt{\frac{1}{N-1}\sum_{n=1}^{N}
\left[d_n(a,c)-\widehat\Delta_N^{(i)}(a,c)\right]^2}.
$$

| Symbol | Definition |
| --- | --- |
| `d_n(a,c)` | Candidate-minus-baseline utility difference for paired sample `n` |
| `\widehat\Delta_N^{(i)}(a,c)` | Sample mean of the paired differences |
| `d_n(a,c)-\widehat\Delta_N^{(i)}(a,c)` | Deviation of one paired difference from the paired sample mean |
| `\operatorname{SE}_{\Delta,N}(a,c)` | Standard error of the paired-difference mean, calculated from the sample variance of the `N` paired differences |
| `N-1` | Degrees-of-freedom denominator used in the unbiased sample-variance estimator |

Pairing reduces the variance of the estimated difference and limits confounding from unequal random-world realisations across actions.

Each trajectory is additionally assigned to one of six mutually exclusive explanatory scenarios: actor backlash, successful target shift, statement accepted, statement contested, speech ignored, or other. The default belief-change threshold is 0.05, and classification follows the stated priority order. These categories support interpretation of the simulated mechanism but do not affect utility calculation or action selection.

### Building a reproducible research instrument

Isolated workers evaluate batches containing all candidates and return only sufficient statistics, preserving paired randomness without transferring full trajectories. Bounded queues provide backpressure, while one writer commits idempotent batches. The request identity covers the game state, actor view, posterior, candidate set, policy, evidence settings, sample count, and seed scheme. Completed requests can be reused; interrupted requests resume with their original sample indices and seeds.

A matrix is complete only when every concrete action has all three evidence cells, every cell reaches its target sample count, scenario counts sum to that count, the matrix baseline exists, paired samples match, and committed sample intervals contain neither gaps nor overlaps. Partial or failed runs are never exposed as complete rankings.

## Result

The implemented pipeline exposes complete action-by-evidence matrices through the command line, graphical interface, and Python API. One production-budget seven-player Seer request evaluated 22 actions across three evidence columns: 66 cells with 100 terminal trajectories each. Its estimated utilities ranged from -0.58 to -0.34. A separate exploratory report covered ten distinct actor-visible perspectives with ten trajectories per cell, totalling 308 action rows and 924 cells.

| Matrix observation | Recorded value | Valid interpretation |
| --- | --- | --- |
| Zero-evidence control | Most actions matched their neutral reference | Ignored speech contributes little through the belief channel. |
| Villager decoy example, high evidence | Matrix baseline -0.4; targeted false Seer claim +0.4; paired difference +0.8 | Replacing the neutral action with this target and claimed result reverses the action-level estimate. |
| Werewolf counterclaim example, high evidence | Matrix baseline +0.6; selected claim +1.0; paired difference +0.4 | Under the fixed policy, the estimated effect varies by target identity; claims directed at a known teammate may reduce utility. |
| Seer with a private Werewolf check | Paired difference +0.4 at medium and high evidence | Truthful revelation produced a positive paired estimate in this information state; the corresponding good-check case did not exhibit the same difference. |

The preliminary results indicate that neither action family nor evidence strength independently determines utility. Estimated utility varies jointly with role-specific information, target, claimed camp, and evidence strength. Greater evidence strength does not produce uniformly higher utility; consequently, aggregation of the Seer-claim action family into a single global value would remove decision-relevant heterogeneity.

The numerical cases are illustrative rather than conclusive. They use ten trajectories per cell, with standard errors of up to approximately 0.33. The cases demonstrate model sensitivity and matrix interpretation but provide no evidence that deception increases win probability in real games.

Reliability evaluation of the matrix implementation yielded deterministic equality across identical requests executed with one, two, and four workers. A deterministic terminal case matched its analytical value. Ten multi-position inputs retained every required evidence cell, and all scenario counts equalled their corresponding sample totals. Input validation rejected complete hidden assignments, inconsistent role knowledge, and illegal actions. Sixty-five module tests passed, covering matrix construction, information isolation, deterministic sampling, persistence, lookup, and interruption recovery.

## Limitations and Next Stage

The current matrix evaluates a defined simulation distribution, not real human or language-model behaviour. The rollout policy is hand-specified, the evidence-strength values are not empirically calibrated, the board and research phase are fixed, and no real-game comparison dataset is yet available. Consequently, the appropriate midterm claim is that the team has built a reproducible and interpretable evaluation instrument, not that it has discovered a universally optimal cheap-talk strategy.

The next stage will record, through the game API, each decision-time public state, lawful actor view, actual speech action, subsequent vote pattern, and terminal result. Real utterances will be mapped to the same structured action representation. Repeated experiments will estimate full-game treatment-versus-control effects, while matched action records will be compared with the matrix's ex ante values and paired action differences. Natural language-model games will be used to assess external applicability. Calibrated belief updates may subsequently replace the current reference evidence settings, and the matrix may be supplied to agents as an interpretable decision instrument. These stages define the proposed progression from mathematical evaluation to empirical observation and explainable agent decision-making.

## Reference

Wang, Shitong. *Optimal Strategy in the Werewolf Game: A Theoretical Study*. arXiv:2408.17177v2, 2025.
