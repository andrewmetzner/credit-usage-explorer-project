# BNL AMU Forecasting and Governance Mathematics

This document exports the focused mathematical specification from the static repository analysis.

## 1. Forecast Weight Selection and Normalization

**Code mapping:** `src/forecasting.py`, `select_auto_weights()` and `normalize_active_weights()`

$$
w_k^{*}=\frac{w_k}{\sum_{j \in A} w_j}
$$

`A` is the set of usable forecast weights: weights that are present, positive, and matched to available components.

## 2. Forecast Component Calculation

**Code mapping:** `src/forecasting.py`, `calculate_forecast_components()`

$$
\text{historical average weekly burn}=\frac{\sum \text{historical weekly total credits used}}{\text{number of historical weekly rows}}
$$

$$
\text{latest week burn}=\text{total credits used in the most recent operational weekly summary row}
$$

$$
\text{recent average burn}=\frac{\sum \text{total credits used in the most recent operational rows}}{\text{number of recent operational rows}}
$$

## 3. Deterministic Forecast Projection

**Code mapping:** `src/forecasting.py`, `calculate_forecast()`

$$
\text{forecast weekly burn}=\sum(\text{normalized forecast weight}\times \text{forecast component})
$$

$$
\text{forecast monthly burn}=\text{forecast weekly burn}\times 4.345
$$

$$
\text{forecast contract-end balance}=\text{credits remaining}-\text{forecast future usage to contract end}
$$

$$
\text{forecast future usage to contract end}=\text{forecast weekly burn}\times \text{weeks remaining}
$$

## 4. Forecast Status Classification

**Code mapping:** `src/forecasting.py`, `calculate_forecast()`

Forecast status values used by the code are `EXHAUSTION_RISK`, `ON_TARGET`, `MODERATE_UNDERUSE`, and `HIGH_UNDERUSE`.

$$
\text{forecast status}=
\begin{cases}
\text{exhaustion risk}, & \text{forecast contract-end balance} < 0 \\
\text{on target}, & 0 \le \text{forecast contract-end balance} \le 50000 \\
\text{moderate underuse}, & 50000 < \text{forecast contract-end balance} \le 150000 \\
\text{high underuse}, & \text{forecast contract-end balance} > 150000
\end{cases}
$$

## 5. Monte Carlo Empirical Multiplier Construction

**Code mapping:** `src/monte_carlo.py`, `build_burn_observations()` and `build_empirical_multipliers()`

$$
\text{burn multiplier}=\frac{\text{observed weekly burn}}{\text{mean observed weekly burn}}
$$

If there are fewer than two clean observations or the mean is not positive, the model falls back to a multiplier of `1.0`.

## 6. Monte Carlo Single Run Simulation

**Code mapping:** `src/monte_carlo.py`, `simulate_one_run()`

$$
\text{simulated weekly burn}=\max(\text{forecast weekly burn}\times \text{sampled burn multiplier},0)
$$

$$
\text{simulated period burn}=\text{simulated weekly burn}\times \text{week fraction}
$$

$$
\text{cumulative future usage}=\text{previous cumulative future usage}+\text{simulated period burn}
$$

$$
\text{contract-end balance}=\text{credits remaining}-\text{cumulative future usage}
$$

## 7. Monte Carlo Summary Metrics

**Code mapping:** `src/monte_carlo.py`, `run_monte_carlo()` and `build_summary()`

$$
\text{exhaustion probability}=\frac{\text{number of exhausted runs}}{\text{total simulation runs}}
$$

$$
\text{stranding probability}=\frac{\text{number of runs with end balance above the stranding threshold}}{\text{total simulation runs}}
$$

## 8. Monte Carlo Risk Status Classification

**Code mapping:** `src/monte_carlo.py`, `build_summary()`

Monte Carlo risk status values used by the code are `HIGH_EXHAUSTION_RISK`, `MODERATE_EXHAUSTION_RISK`, `HIGH_STRANDING_RISK`, `MODERATE_STRANDING_RISK`, and `BALANCED_RISK`.

$$
\text{risk status}=
\begin{cases}
\text{high exhaustion risk}, & \text{exhaustion probability} \ge 0.50 \\
\text{moderate exhaustion risk}, & 0.20 \le \text{exhaustion probability} < 0.50 \\
\text{high stranding risk}, & \text{exhaustion probability} < 0.20 \text{ and stranding probability} \ge 0.50 \\
\text{moderate stranding risk}, & \text{exhaustion probability} < 0.20 \text{ and } 0.20 \le \text{stranding probability} < 0.50 \\
\text{balanced risk}, & \text{otherwise}
\end{cases}
$$

## 9. Cap Pressure User Detail

**Code mapping:** `src/cap_pressure.py`, `build_user_detail()`

$$
\text{cap utilization}=\frac{\text{credits used}}{\text{weekly credit cap}}
$$

$$
\text{remaining weekly credits}=\text{weekly credit cap}-\text{credits used}
$$

## 10. Pressure Flag Classification

**Code mapping:** `src/cap_pressure.py`, `assign_pressure_flag()`

Pressure flag values used by the code are `ABOVE_CAP_110_PLUS`, `AT_OR_ABOVE_CAP`, `HIGH_PRESSURE_90_PLUS`, `ELEVATED_PRESSURE_80_PLUS`, and `NORMAL`.

$$
\text{pressure flag}=
\begin{cases}
\text{above cap 110 plus}, & \text{cap utilization} \ge 1.10 \\
\text{at or above cap}, & 1.00 \le \text{cap utilization} < 1.10 \\
\text{high pressure 90 plus}, & 0.90 \le \text{cap utilization} < 1.00 \\
\text{elevated pressure 80 plus}, & 0.80 \le \text{cap utilization} < 0.90 \\
\text{normal}, & \text{cap utilization} < 0.80
\end{cases}
$$

## 11. Concentration Metrics and Cap Pressure Index

**Code mapping:** `src/cap_pressure.py`, `calculate_top_share()`, `calculate_gini()`, `calculate_hhi()`, `calculate_cap_pressure_index()`

$$
\text{top share}=\frac{\text{sum of top-\(N\) usage values}}{\text{total usage}}
$$

$$
\text{HHI}=\sum \left(\frac{\text{user usage}}{\text{total usage}}\right)^2
$$

$$
\text{cap pressure index}=100\times(\text{weighted utilization}+\text{weighted threshold pressure}+\text{weighted concentration pressure})
$$

The code clips the final score to the interval `[0, 100]`.

## 12. Cap Pressure Summary Aggregation

**Code mapping:** `src/cap_pressure.py`, `build_summary()` and `build_tier_summary()`

$$
\text{average credits per user}=\frac{\text{total credits used}}{\text{number of user rows}}
$$

$$
\text{tier share of total credits}=\frac{\text{tier total credits used}}{\text{all tier total credits used}}
$$

## 13. Historical Cap Pressure Summary and Recommendation Signal

**Code mapping:** `src/cap_pressure_history.py`, `build_user_summary()` and `assign_tier_recommendation()`

$$
\text{share of weeks over 90 percent cap}=\frac{\text{weeks over 90 percent cap}}{\text{weeks observed}}
$$

$$
\text{pressure trend change}=\text{latest cap utilization}-\text{first cap utilization}
$$

Legacy recommendation rules:

Legacy recommended action values used by the code are `REVIEW_EMERGENCY_OVERRIDE`, `MONITOR_MORE_HISTORY_NEEDED`, `CONSIDER_MOVE_UP_TIER`, `CONSIDER_MOVE_DOWN_TIER`, `MONITOR_RECENT_SPIKE`, and `NO_CHANGE`.

$$
\text{recommended action}=
\begin{cases}
\text{review emergency override}, & \text{emergency flag is true} \\
\text{monitor more history needed}, & \text{weeks observed} < 2 \\
\text{consider move up tier}, & \text{weeks observed} \ge 3 \text{ and share over 90 percent cap} \ge 0.50 \\
\text{consider move down tier}, & \text{weeks observed} \ge 4 \text{ and average/latest utilization} \le 0.25 \\
\text{monitor recent spike}, & \text{latest utilization} \ge 0.90 \text{ and share over 90 percent cap} < 0.50 \\
\text{no change}, & \text{otherwise}
\end{cases}
$$

## 14. Legacy Tier Recommendation Outputs

**Code mapping:** `src/tier_recommendations.py`, `build_tier_recommendations()`

$$
\text{recommended cap change}=\text{recommended weekly credit cap}-\text{latest weekly credit cap}
$$

$$
\text{estimated average utilization after change}=\frac{\text{average weekly credits used}}{\text{recommended weekly credit cap}}
$$

## 15. Policy Scenario Contract Sizing

**Code mapping:** `src/policy_scenario_sandbox.py`, `calculate_dynamic_contract_size()` and `calculate_burn_adjustment_factor()`

$$
\text{required contract size}=\text{current contract size}+\left|\min(\text{forecast contract-end balance},0)\right|
$$

$$
\text{dynamic contract size}=\text{required contract size}\times\left(1+\frac{\text{buffer percent}}{100}\right)
$$

$$
\text{burn adjustment factor}=\max(0,1-\text{total cap effect})
$$

$$
\text{projected contract-end balance}=\text{scenario purchased credits}-\text{total credits used}-\text{future usage}
$$

## 16. Policy Scenario Risk Estimate

**Code mapping:** `src/policy_scenario_sandbox.py`, `estimate_risk()`

$$
\text{estimated exhaustion probability}=
\begin{cases}
\text{baseline exhaustion probability}, & \text{scenario balance} < 0 \text{ and baseline balance has no usable denominator} \\
\min\left(1,\text{baseline exhaustion probability}\times \frac{|\text{scenario balance}|}{|\text{baseline balance}|}\right), & \text{scenario balance} < 0 \text{ and baseline balance denominator is positive} \\
0, & \text{scenario balance} \ge 0
\end{cases}
$$

$$
\text{estimated stranding probability}=
\begin{cases}
\min\left(1,\frac{\text{scenario balance}}{\text{scenario purchased credits}}\right), & \text{scenario balance} > 0 \text{ and purchased credits} > 0 \\
0, & \text{otherwise}
\end{cases}
$$

## 17. Policy Scenario Status Classification

**Code mapping:** `src/policy_scenario_sandbox.py`, `determine_scenario_status()` and `determine_balance_status()`

Scenario status values used by the code are `CRITICAL`, `WARNING`, `OVERSIZED`, and `BALANCED`.

$$
\text{scenario status}=
\begin{cases}
\text{critical}, & \text{estimated exhaustion probability} \ge \text{high exhaustion threshold} \\
\text{warning}, & \text{estimated exhaustion probability} \ge \text{moderate exhaustion threshold} \\
\text{oversized}, & \text{estimated stranding probability} \ge \text{high stranding threshold} \\
\text{balanced}, & \text{otherwise}
\end{cases}
$$

Balance status values used by the code are `DEFICIT`, `SURPLUS`, and `NEAR_TARGET`.

$$
\text{balance status}=
\begin{cases}
\text{deficit}, & \text{projected balance} < \text{lower target bound} \\
\text{surplus}, & \text{projected balance} > \text{upper target bound} \\
\text{near target}, & \text{otherwise}
\end{cases}
$$

## 18. Recommendation Engine User Scoring

**Code mapping:** `src/policy_recommendation/users.py`, `_recommend_one()`

$$
\text{move-up score}=
\text{move-up pressure weight}\times \mathbb{1}(\text{move-up signal})+
\text{historical heavy-user weight}\times \mathbb{1}(\text{historical heavy signal})
$$

$$
\text{move-down score}=
\text{move-down pressure weight}\times \mathbb{1}(\text{move-down signal})+
\text{historical light-user weight}\times \mathbb{1}(\text{historical light signal})
$$

$$
\text{review score}=
\text{emergency weight}\times \mathbb{1}(\text{emergency signal})+
\text{history weight}\times \mathbb{1}(\text{limited history signal})+
\text{spike weight}\times \mathbb{1}(\text{spike signal})
$$

Package action values used by the code are `REVIEW`, `MOVE_UP`, `MOVE_DOWN`, and `MAINTAIN`.

$$
\text{recommended action}=
\begin{cases}
\text{review}, & \text{review score is positive and at least as large as the other scores} \\
\text{move up}, & \text{move-up score is largest} \\
\text{move down}, & \text{move-down score is largest} \\
\text{maintain}, & \text{otherwise}
\end{cases}
$$

## 19. Recommendation Engine Tier Transition and Credit Impact

**Code mapping:** `src/policy_recommendation/users.py`, `_transition_user_tier()` and `_build_credit_impact_summary()`

$$
\text{recommended tier}=
\begin{cases}
\text{next higher tier}, & \text{action is move up and a higher tier exists} \\
\text{next lower tier}, & \text{action is move down and a lower tier exists} \\
\text{current tier}, & \text{otherwise}
\end{cases}
$$

$$
\text{estimated credit impact}=\text{recommended credit cap}-\text{current credit cap}
$$

$$
\text{net credit impact}=\text{recommended total estimated credit impact}-\text{current total estimated credit impact}
$$

## 20. Recommendation Confidence

**Code mapping:** `src/policy_recommendation/utils.py`, `calculate_confidence()`

$$
\text{confidence ratio}=\frac{\text{score}}{\text{maximum score}}
$$

Confidence labels used by the code are `HIGH`, `MEDIUM`, and `LOW`.

$$
\text{confidence}=
\begin{cases}
\text{high}, & \text{confidence ratio} \ge 0.70 \\
\text{medium}, & 0.40 \le \text{confidence ratio} < 0.70 \\
\text{low}, & \text{confidence ratio} < 0.40 \text{ or maximum score} \le 0
\end{cases}
$$

## Notes

- These equations are faithful mathematical restatements of the Python implementation.
- Thresholds and constants are preserved exactly as written in the code and YAML files.
- This export is static-analysis only and does not include any executed results.
