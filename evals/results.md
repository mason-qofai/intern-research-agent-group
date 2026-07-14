## Confusion Matrix

```
Expected → Predicted

           HIGH  MEDIUM  LOW  TOTAL
HIGH        17      2     0    19
MEDIUM       2     12     1    15
LOW          1      5     9    15

TOTAL       20     19    10    49
```


# Confidence Scorer Evaluation Report

**Date:** July 13, 2026  
**Model:** claude-opus-4-6  
**Evaluation Set:** 50 test cases

---

## Executive Summary

The confidence-scorer skill achieved 78.0% overall accuracy (39/50 correct predictions). Performance meets the 70% deployment threshold.

---

## Overall Performance Metrics

| Metric | Value |
|--------|-------|
| Overall Accuracy | 78.0% (39/50) |
| Total Errors | 11 |

---

## Per-Class Performance

### HIGH Confidence (19 cases)
- **Accuracy:** 89% (17/19 correct)
- **Errors:** 2 (2 predicted MEDIUM, 0 predicted LOW)
- **Strength:** Strongest class. Model correctly identifies verifiable, fact-based claims.

### MEDIUM Confidence (15 cases)
- **Accuracy:** 75% (13/16 correct)
- **Errors:** 3 (2 predicted HIGH, 1 predicted LOW)
- **Strength:** Strong performance. Model handles claims mixing verifiable and subjective elements.

### LOW Confidence (15 cases)
- **Accuracy:** 60% (9/15 correct)
- **Errors:** 6 (1 predicted HIGH, 5 predicted MEDIUM)
- **Weakness:** Lowest performance. Model tends to upgrade LOW claims to MEDIUM.


