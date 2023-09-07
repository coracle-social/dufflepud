import boto3, re
from raddoo import env, first


def get_original_name(f):
    filename = getattr(f, 'filename', None) or getattr(f, 'name', None)

    if filename is None:
        return None

    if isinstance(filename, bytes):
        filename = filename.decode('utf-8')

    # S3 doesn't like non-ascii meta
    return filename.encode('ascii', 'ignore').decode('utf-8')


BUCKET = env('S3_BUCKET')

config = {
    "aws_access_key_id": env('S3_ACCESS_KEY_ID'),
    "aws_secret_access_key": env('S3_SECRET_ACCESS_KEY'),
    "endpoint_url": env('S3_ENDPOINT_URL'),
}

client = boto3.client('s3', **config)
resource = boto3.resource('s3', **config)
bucket = resource.Bucket(BUCKET)


def put(key, file_obj, content_type):
    bucket.put_object(
        Key=key,
        Body=file_obj,
        ContentType=content_type,
        ACL='public-read'
    )


def delete(key):
    bucket.delete_objects(Delete={'Objects': [{'Key': key}]})


def get_url(key):
    url = client.generate_presigned_url(
        ClientMethod='get_object',
        Params={'Bucket': BUCKET, 'Key': key}
    )

    # Remove the signature, we're saving things with a public ACL
    return first(url.rsplit('?', 1))


def get_all_objects():
    page = None
    load_more = True
    results = []

    while load_more:
        if page:
            page = client.list_objects_v2(Bucket=BUCKET, Prefix='uploads/', StartAfter=page['NextContinuationToken'])
        else:
            page = client.list_objects_v2(Bucket=BUCKET, Prefix='uploads/')

        load_more = len(page['Contents']) >= 1000
        results.extend(page['Contents'])

    return results


def get_moderation_list():
    media = []
    for i, x in enumerate(sorted(get_all_objects(), key=lambda x: -x['LastModified'].timestamp())):
        url = get_url(x['Key'])
        ext = url.split('.')[-1]

        media.append({'url': url, 'ext': ext, 'key': x['Key']})

    return media
