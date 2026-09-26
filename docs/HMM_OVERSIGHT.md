# HMM oversight assessment for Larry

Recommendation: investigate an HMM as a **shadow reviewer**, after operational errors and performance-accounting issues are addressed. Do not let an unvalidated model change orders or disable firm stops.

## Source reviewed

Jurafsky and Martin, *Speech and Language Processing*, Appendix A, **Hidden Markov Models**, draft dated August 19, 2026: [Stanford PDF](https://web.stanford.edu/~jurafsky/slp3/A.pdf).

The chapter separates observed measurements from hidden states and describes transition/emission probabilities. Its forward algorithm computes sequence likelihood; Viterbi finds a best state sequence; forward-backward/Baum-Welch estimates model parameters. First-order state dependence and conditional observation independence are simplifying assumptions. These methods explain inference mechanics, not evidence that HMMs predict profitable BTC trades.

## Proposed application — our design, not a result claimed by the chapter

Use completed hourly bars to describe latent market conditions. Begin with two or three statistical states. Name them only after inspecting their learned behavior; do not force “bull”, “bear” and “chop” labels onto clusters that merely separate volatility levels.

Candidate inputs: log return, trailing realized volatility, normalized range/ATR and volume surprise. Keep the feature set small because RSI, stochastic RSI and Bollinger measures are correlated transformations of price. Fit scaling parameters only on the training window. Market-data health remains a deterministic monitor; an HMM is not a substitute for heartbeat, funding, reconciliation or execution checks.

Show: current state probabilities, probability of transition, uncertainty/entropy, observation likelihood relative to the training distribution, model version, training cutoff and data freshness. Attach the recorded probabilities to every simulated decision for later review. Example question: “Does Larry lose more often when its existing macro label disagrees with a high-confidence statistical state?” This is an analysis question, not an automatic trading rule.

## Preventing hindsight

Live shadow labels must use filtered probabilities from observations available up to that completed bar. Full-sequence smoothing and Viterbi paths can revise earlier labels using later observations; reserve them for clearly labeled retrospective analysis. Save the original timestamped live probabilities so reports cannot silently replace them with hindsight labels.

Train offline on chronological market data, not on Larry's single closed trade. Use rolling or expanding training windows followed by untouched validation/test windows. Multiple initializations, fixed model artifacts and stable post-fit state mapping make comparisons reproducible. Use numerically stable likelihood computation. Consider Gaussian emissions for continuous features as an extension; the chapter's illustrative categorical observations are not a ready-made financial implementation.

## Evidence required before any decision authority

- Compare against Larry's existing SMA macro filter and a simple volatility-only baseline.
- Evaluate state stability, occupancy, dwell time, transition frequency and out-of-sample likelihood.
- Group actual paper results by the probabilities recorded at entry; report counts, fees, funding, drawdown and uncertainty.
- Predefine any hypothetical filter, evaluate it with walk-forward replay, and include missed winners and all trading costs.
- Stress test missing candles, restarts, out-of-distribution volatility and model unavailability. Shadow failure must leave Larry's deterministic risk controls intact.

No HMM was trained or deployed in this audit. A single losing trade is insufficient evidence for choosing or validating a regime-dependent trading policy. The useful next deliverable is a reproducible offline shadow experiment, followed by time-stamped observation-only reporting if the results justify it.
