-- Run these in the Athena console (or `aws athena start-query-execution`)
-- against workgroup "weather-pipeline-wg", database "weather_pipeline_db",
-- table "observations".
--
-- No MSCK REPAIR TABLE / crawler run needed -- the table uses partition
-- projection, so Athena computes valid dt=/hour= partitions on the fly
-- from the query's WHERE clause instead of reading them from a catalog.

-- 1. Average temperature per city over the last 7 days.
SELECT
    city,
    round(avg(temperature_c), 1) AS avg_temp_c,
    count(*) AS observations
FROM weather_pipeline_db.observations
WHERE dt >= date_format(current_date - INTERVAL '7' DAY, '%Y-%m-%d')
GROUP BY city
ORDER BY avg_temp_c DESC;

-- 2. How many records landed per hour today (sanity-check the pipeline
--    is actually running on schedule).
SELECT
    dt,
    hour,
    count(*) AS record_count
FROM weather_pipeline_db.observations
WHERE dt = date_format(current_date, '%Y-%m-%d')
GROUP BY dt, hour
ORDER BY hour;

-- 3. Windiest city on record.
SELECT
    city,
    max(windspeed_kmh) AS max_windspeed_kmh
FROM weather_pipeline_db.observations
GROUP BY city
ORDER BY max_windspeed_kmh DESC;

-- 4. Latest observation per city (window function over all partitions).
SELECT city, temperature_c, windspeed_kmh, observation_time
FROM (
    SELECT
        city,
        temperature_c,
        windspeed_kmh,
        observation_time,
        row_number() OVER (PARTITION BY city ORDER BY fetched_at DESC) AS rn
    FROM weather_pipeline_db.observations
)
WHERE rn = 1;
