import boto3
from raddoo import env, first, spit


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


if __name__ == '__main__':
    objects = sorted(
        client.list_objects(Bucket=BUCKET, Prefix='uploads/')['Contents'],
        key=lambda x: -x['LastModified'].timestamp()
    )

    print(f"{len(objects)} objects found")

    items = []
    for i, x in enumerate(objects):
        if i > 1000:
            break

        items.append(f"""
        <div style="padding-top: 10px">
            <div>{x['Key']}</div>
            <img width="320" src="{get_url(x['Key'])}" />
        </div>
        """)

    html = f"""
    <div style="display: grid; gap: 20px; grid-template-columns: 1fr 1fr 1fr;">
        <div>{'</div><div>'.join(items)}</div>
    </div>
    """

    spit('objects.html', html)
