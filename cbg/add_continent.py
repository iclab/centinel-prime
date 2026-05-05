import pandas as pd
import pycountry_convert as pc
import geonamescache   
import country_converter as coco  
 
def country_to_continent(country_code: str) -> str:
    try:
        # Convert country code to continent code
        continent_code = pc.country_alpha2_to_continent_code(country_code)
        # Convert continent code to continent name
        continent_name = pc.convert_continent_code_to_continent_name(continent_code)
        return continent_name
    except: 
        return "Unknown"
    
def country_to_continent_geonames(country_code: str) -> str:
    # Using geonamescache as verification
    gc = geonamescache.GeonamesCache()
    countries = gc.get_countries()
    
    for country_data in countries.values():
        if country_data['iso'] == country_code:
            continent_code = country_data['continentcode']
            continent_mapping = {
                'AF': 'Africa',
                'AS': 'Asia',
                'EU': 'Europe',
                'NA': 'North America',
                'OC': 'Oceania',
                'SA': 'South America',
                'AN': 'Antarctica'
            }
            return continent_mapping.get(continent_code, "Unknown")
    return "Unknown"

def country_to_continent_coco(country_code: str) -> str:
    try:
        # Convert to ISO3 temporarily as coco works better with ISO3
        cc = coco.CountryConverter()
        continent = cc.convert(country_code, to='continent')
        return continent
    except:
        return "Unknown"

def add_continent_column(landmarks_path: str) -> str:
    df = pd.read_csv(landmarks_path) 
    if 'continent' not in df.columns:
        df['continent'] = df['country'].apply(country_to_continent)
        df.to_csv(landmarks_path, index=False)
        return "'contient' column is added"
    else:
        return "'continent' colum already exists. Skipping write."
    
  