import json
import boto3
import requests
import numpy as np
import urllib.parse
import rasterio.mask
from datetime import date, timedelta, datetime
from rasterio.warp import calculate_default_transform, reproject
from rasterio.enums import Resampling
from shapely.geometry import box, Polygon
from pyproj import Proj, CRS, Transformer
import os
import tempfile

# Environment Variables
stac_api_endpoint = os.environ.get("STAC_API_ENDPOINT", "https://earth-search.aws.element84.com/v1/search")
bucket_out = os.environ.get("BUCKET_OUT", "sentinel-2-cogs-rnil")
sns_topic_arn = os.environ.get("SNS_TOPIC_ARN", "arn:aws:sns:us-west-2:268065301848:NoData-Sentinel-API")
state_machine_arn = os.environ.get("STATE_MACHINE_ARN", 'arn:aws:states:us-west-2:268065301848:stateMachine:sentinel-2-data-calculate')

# AWS Clients
s3 = boto3.client('s3')
sns = boto3.client('sns')
sfn = boto3.client('stepfunctions')

# Remote Sensing Indices
formula_dict = {
    'NDVI': ['red', 'nir'],
    'NDMI': ['nir08', 'swir16']
}



def get_bbox_and_coords_from_geojson(geojson_str):
    
    
    geojson_data = json.loads(geojson_str)
    
    coordinates = geojson_data['geometry']['coordinates']
    
    # Flatten the list of coordinates
    flattened_coords = [item for sublist in coordinates for item in sublist]
    # Extract min and max values for each dimension
    min_x, min_y = min(coord[0] for coord in flattened_coords), min(coord[1] for coord in flattened_coords)
    max_x, max_y = max(coord[0] for coord in flattened_coords), max(coord[1] for coord in flattened_coords)
    
    # Return lower left and upper right coordinates
    bbox = [min_x, min_y, max_x, max_y]
    
    
    return bbox, coordinates
    
def clipper(sentinel_band_url, shapes, clipped_band):
    response = requests.get(sentinel_band_url)
    
    with tempfile.NamedTemporaryFile(suffix='.tif', delete=False) as temp_band:
        temp_band.write(response.content)
        temp_band_path = temp_band.name

    with rasterio.open(temp_band_path) as src:
        out_image, out_transform = rasterio.mask.mask(src, [shapes], crop=True, all_touched=True)
        out_meta = src.meta.copy()

    out_meta.update({
        "height": out_image.shape[1],
        "width": out_image.shape[2],
        "transform": out_transform
    })

    with tempfile.NamedTemporaryFile(suffix='.tif', delete=False) as temp_clipped_band:
        with rasterio.open(temp_clipped_band.name, 'w', **out_meta) as dst:
            dst.nodata = -9999   
            dst.write(out_image.astype(rasterio.uint16))
        return temp_clipped_band.name

def write_tiff_and_upload(upload_details):
    arr = upload_details['dataArray']
    meta = upload_details['metaData']
    filetiff = upload_details['fileNameToUpload']

    with tempfile.NamedTemporaryFile(suffix='.tif', delete=False) as temp_tiff:
        with rasterio.open(temp_tiff.name, 'w', **meta) as dst:
            dst.nodata = -9999
            dst.write(arr.astype(rasterio.float32), 1)
        s3.upload_file(temp_tiff.name, bucket_out, filetiff)
    

def calculate_data(index_name, band_list, meta_details):
    fileName = meta_details['fileName']
    sensing_date = meta_details['sensingDate']
    shapes = meta_details['UTMshape']
    assets = meta_details['asset_data']

    # Initialize empty list to store temporary file paths
    temp_file_paths = []

    # Clip the individual bands and store the temporary file paths
    for item in band_list:
        sentinel_band_url = assets[item]['href']
        temp_file_path = clipper(sentinel_band_url, shapes, item)
        temp_file_paths.append(temp_file_path)

    # Read the bands from the temporary files
    with rasterio.open(temp_file_paths[0]) as src:
        bandA = src.read(1)
        index_meta = src.meta.copy()

    with rasterio.open(temp_file_paths[1]) as src:
        bandB = src.read(1)

    # Update metadata for the index raster
    index_meta.update(
        dtype=rasterio.float32,
        count=1,
        compress='lzw'
    )

    # Perform raster calculation
    calc_index_array = (bandB.astype(float) - bandA.astype(float)) / (bandB + bandA)

    fileToUpload = f"{fileName}/{sensing_date}_{index_name}.tif"

    upload_details = {
        "dataArray": calc_index_array,
        "metaData": index_meta,
        "fileNameToUpload": fileToUpload,
    }

    write_tiff_and_upload(upload_details)

    # Optionally, remove temporary files if you don't need them anymore
    for temp_file_path in temp_file_paths:
        os.remove(temp_file_path)

    return "Success"


# Function to get the next available execution name
def get_next_execution_name(base_name,st_arn):
    
    execution_name = base_name
    counter = 1
    while True:
        try:
            sfn.describe_execution(
                executionArn=f"{st_arn}:{execution_name}".replace("stateMachine","execution")
            )
            execution_name = f"{base_name}_{counter}"
            counter += 1
        except sfn.exceptions.ExecutionDoesNotExist:
           
            return execution_name    
    

def lambda_handler(event, context):
    
    print(event)
    bucket_input = event['Records'][0]['s3']['bucket']['name']
    key_in = event['Records'][0]['s3']['object']['key']
    
    # Decode the URL-encoded key
    key = urllib.parse.unquote_plus(key_in)
    print(key, key_in)
    
    
    response = s3.get_object(Bucket=bucket_input, Key=key)
        
    geojson_str = response['Body'].read().decode('utf-8')
    
    # Extract bounding box from GeoJSON file
    bbox,coords = get_bbox_and_coords_from_geojson(geojson_str)
    
    
    now = datetime.now()
    yesterday = now - timedelta(days=6)

    time_range = f"{yesterday.strftime('%Y-%m-%d')}T{now.strftime('%H:%M:%S')}Z/{now.strftime('%Y-%m-%d')}T{now.strftime('%H:%M:%S')}Z"
    print(f"Query data for {time_range}")
   
    #Creating header and payload for post request to element84
    
    headers = {'Content-Type': 'application/json','Accept': 'application/geo+json'}
    
    payload = {
        
        "bbox": bbox,
        "collections": ["sentinel-2-l2a"],
        "datetime": time_range,
        "limit": 1
    }
    
    #Hitting the STAC api version 2 by element84
    response = requests.post(stac_api_endpoint, data = json.dumps(payload), headers = headers)
    data = response.json()
    
    

    try :
        utm_epsg , utm_zone, sensing_date = "EPSG:"+str(data["features"][0]["properties"]["proj:epsg"]) , data["features"][0]["properties"]['mgrs:utm_zone'] , data["features"][0]["properties"]["created"]
        print(f"Sensing_date : {sensing_date}")
    except:
        
        topic_arn = "arn:aws:sns:us-west-2:268065301848:NoData-Sentinel-API"
        
        msg = f"No data from Sentinel satellite on {time_range} for farm name : {key}"
        
        response = sns.publish(
            TopicArn=topic_arn,
            Message=msg,
            Subject = "NoData from Sentinel-2"
            )
        
        return event
        
    else:
        
        utm , wgs84 = CRS.from_string(utm_epsg) , CRS.from_string('EPSG:4326')
        project = Transformer.from_crs(wgs84, utm, always_xy=True)

      

        utm = []
        for item in coords[0]:
            utm_pt = project.transform(item[0],item[1])
            utm.append(utm_pt)

        
        utm_polygon = Polygon(utm)

        assets = data["features"][0]["assets"]
    
    
        meta_details = {
            "fileName" : key[:-8],
            "sensingDate" : sensing_date.split("T")[0],
            "UTMshape" : utm_polygon,
            "asset_data" : assets
        }
    
        for ky,value in formula_dict.items():
        
            msg = calculate_data(ky,value,meta_details)
        
        print(sensing_date)
        # Parse the full date and time from the sensing_date string
        date2 = datetime.strptime(sensing_date, '%Y-%m-%dT%H:%M:%S.%fZ')

    
        difference = now - date2

        
        seconds_difference = int(difference.total_seconds())

        print(f"Now : {now} , Sensing : {date2} , Difference : {seconds_difference}")
        
        stepfunctiondata = {

            "input_data" : {
                "coords" : coords[0],
                "payload" : payload,
                "key" : key,},
            "wait_duration_seconds" : seconds_difference
        }
        
        state_machine_arn = 'arn:aws:states:us-west-2:268065301848:stateMachine:sentinel-2-data-calculate'
        
        try:
            execution_name = get_next_execution_name(f"{key[:-8]}".replace(" ", "_"),state_machine_arn)
            print(f"Initializing step functions :{execution_name}")
            response = sfn.start_execution(
                stateMachineArn=state_machine_arn,
                input=json.dumps(stepfunctiondata),
                name=execution_name
            )
            print(response)
        except sfn.exceptions.ExecutionLimitExceeded as e:
            # Handle the case where you've reached the execution limit
            print(f"Execution limit exceeded: {e}")
        except Exception as e:
            # Handle other exceptions as needed
            print(f"An error occurred: {e}")
       
    
    return f"####---- data successfully cretead for {key} -----#####"

    
    
    
    
    
    