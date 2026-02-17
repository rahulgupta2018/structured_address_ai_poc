-- SQLite
SELECT * FROM geonames_city_names WHERE normalized_name = 'springfield' LIMIT 10;

-- Disambiguate with postal code
SELECT * FROM geonames_postal_codes WHERE country_code = 'US' AND postal_code = '62701';


-- Most populated cities in a country
SELECT name, population, admin1_code FROM geonames_cities 
WHERE country_code = 'IN' ORDER BY population DESC LIMIT 20;


-- Check which tables exist
SELECT name FROM sqlite_master WHERE type='table';