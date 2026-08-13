-- 通常校正25本と不足P型拡張5本の統合集計
-- BigQuery Standard SQL

WITH all_review AS (
  SELECT
    calibration_id, prior_primary_structure human_p, manus_primary_structure manus_p,
    prior_director_card human_r, manus_director_card manus_r,
    prior_food_role human_food, manus_food_role manus_food, confidence
  FROM `rugged-destiny-408613.senior_reading_all.manus_calibration_review_v1`
  WHERE manus_primary_structure IS NOT NULL
  UNION ALL
  SELECT
    calibration_id, prior_primary_structure, manus_primary_structure,
    prior_director_card, manus_director_card,
    prior_food_role, manus_food_role, confidence
  FROM `rugged-destiny-408613.senior_reading_all.manus_calibration_extension_review_v1`
  WHERE manus_primary_structure IS NOT NULL
)
SELECT
  COUNT(*) completed,
  COUNTIF(human_p=manus_p) primary_agree,
  COUNTIF(human_r=manus_r) director_card_agree,
  COUNTIF(human_food=manus_food) food_role_agree,
  COUNTIF(human_p=manus_p AND human_r=manus_r AND human_food=manus_food) exact_agree,
  ROUND(AVG(confidence),3) avg_confidence
FROM all_review;

WITH all_review AS (
  SELECT prior_primary_structure human_value, manus_primary_structure manus_value
  FROM `rugged-destiny-408613.senior_reading_all.manus_calibration_review_v1`
  WHERE manus_primary_structure IS NOT NULL
  UNION ALL
  SELECT prior_primary_structure, manus_primary_structure
  FROM `rugged-destiny-408613.senior_reading_all.manus_calibration_extension_review_v1`
  WHERE manus_primary_structure IS NOT NULL
)
SELECT human_value, manus_value, COUNT(*) n
FROM all_review GROUP BY 1,2 ORDER BY 1,2;

