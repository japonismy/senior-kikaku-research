-- Manus校正セット: 完了数・一致率・混同行列
-- BigQuery Standard SQL

SELECT
  COUNT(*) AS completed,
  COUNTIF(primary_agreement) AS primary_agree,
  COUNTIF(card_agreement) AS director_card_agree,
  COUNTIF(food_role_agreement) AS food_role_agree,
  COUNTIF(primary_agreement AND card_agreement AND food_role_agreement) AS exact_agree,
  COUNTIF(needs_human_calibration_review) AS open_review,
  ROUND(AVG(confidence), 3) AS avg_confidence
FROM `rugged-destiny-408613.senior_reading_all.manus_calibration_review_v1`
WHERE manus_primary_structure IS NOT NULL;

SELECT
  prior_primary_structure AS human_value,
  manus_primary_structure AS manus_value,
  COUNT(*) AS n
FROM `rugged-destiny-408613.senior_reading_all.manus_calibration_review_v1`
WHERE manus_primary_structure IS NOT NULL
GROUP BY 1, 2
ORDER BY 1, 2;

SELECT
  prior_director_card AS human_value,
  manus_director_card AS manus_value,
  COUNT(*) AS n
FROM `rugged-destiny-408613.senior_reading_all.manus_calibration_review_v1`
WHERE manus_primary_structure IS NOT NULL
GROUP BY 1, 2
ORDER BY 1, 2;

SELECT
  prior_food_role AS human_value,
  manus_food_role AS manus_value,
  COUNT(*) AS n
FROM `rugged-destiny-408613.senior_reading_all.manus_calibration_review_v1`
WHERE manus_primary_structure IS NOT NULL
GROUP BY 1, 2
ORDER BY 1, 2;
