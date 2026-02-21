"""Upload an HTML file from the responses/ directory to S3 and return an openable URL.

This module exposes a single function `upload_html_and_get_object_url(name, ...)`
which expects `name` to reference a file under the `responses/` directory (or
an absolute/path-containing filename). The function uploads the file and
returns either a public object URL or a presigned URL when the object is
private or when the bucket does not allow ACLs.

CLI usage (keeps backward-compatible behavior):
  - If you pass a path to an HTML file, the CLI copies it into `responses/` and
    then calls the function with the saved name.
  - If you pipe HTML to stdin, the CLI writes it to `responses/<generated>.html`.

Return value: dict with keys {"success": bool, "url": str, "detail"/"error"}
"""

from __future__ import annotations

import os
import time
import uuid
import shutil
import json
from typing import Optional, Dict, Any

import boto3
from botocore.exceptions import ClientError


def upload_html_and_get_object_url(
    name: str,
    bucket: Optional[str] = None,
    key: Optional[str] = None,
    make_public: bool = True,
    presign_if_private: bool = True,
    presign_expires: int = 3600,
    aws_access_key_id: Optional[str] = None,
    aws_secret_access_key: Optional[str] = None,
    aws_region: Optional[str] = None,
    responses_dir: str = "responses",
) -> Dict[str, Any]:
    """Upload the HTML file found at responses/<name> to S3 and return a URL.

    `name` may be a basename (e.g. 'myfile.html') or a path. If it is a basename
    the function will look inside `responses_dir` for the file.
    """
    # load .env if available (so environment variables like AWS_ACCESS_KEY_ID can be read)
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    bucket = bucket or os.environ.get("S3_BUCKET")
    if not bucket:
        return {"success": False, "error": "S3 bucket not provided (arg or S3_BUCKET env)"}

    # ensure name ends with .html
    name = name if name.lower().endswith('.html') else name + '.html'

    # determine file path
    if os.path.isabs(name) or os.path.sep in name:
        path = name
    else:
        path = os.path.join(responses_dir, name)

    if not os.path.exists(path):
        return {"success": False, "error": f"HTML file not found at {path}"}

    try:
        with open(path, 'r', encoding='utf-8') as fh:
            html = fh.read()
    except Exception as e:
        return {"success": False, "error": f"Failed reading HTML file: {e}"}

    # construct object key
    if key:
        key = key if key.lower().endswith('.html') else key + '.html'
    else:
        key = f"panikbot/{int(time.time())}-{uuid.uuid4().hex}.html"

    # resolve credentials/region
    aws_access_key_id = aws_access_key_id or os.environ.get('AWS_ACCESS_KEY_ID')
    aws_secret_access_key = aws_secret_access_key or os.environ.get('AWS_SECRET_ACCESS_KEY')
    aws_region = aws_region or os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION')

    client_kwargs: Dict[str, Any] = {}
    if aws_access_key_id and aws_secret_access_key:
        client_kwargs['aws_access_key_id'] = aws_access_key_id
        client_kwargs['aws_secret_access_key'] = aws_secret_access_key
    if aws_region:
        client_kwargs['region_name'] = aws_region

    s3 = boto3.client('s3', **client_kwargs)

    extra_args = {'ContentType': 'text/html'}
    if make_public:
        extra_args['ACL'] = 'public-read'

    # Try to upload
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=html.encode('utf-8'), **extra_args)
    except ClientError as e:
        code = getattr(e, 'response', {}).get('Error', {}).get('Code')
        # If ACLs are not supported, retry without ACL and return presigned URL if requested
        if code == 'AccessControlListNotSupported' and make_public:
            try:
                s3.put_object(Bucket=bucket, Key=key, Body=html.encode('utf-8'), ContentType='text/html')
                if presign_if_private:
                    url = s3.generate_presigned_url(
                        ClientMethod='get_object',
                        Params={'Bucket': bucket, 'Key': key},
                        ExpiresIn=presign_expires,
                    )
                    return {"success": True, "url": url, "detail": "uploaded_no_acl_presigned_url"}
            except ClientError as e2:
                return {"success": False, "error": str(e2), "code": getattr(e2, 'response', {}).get('Error', {}).get('Code')}
        return {"success": False, "error": str(e), "code": code}

    # If upload succeeded and caller wants a presigned link for private objects
    if not make_public and presign_if_private:
        url = s3.generate_presigned_url(
            ClientMethod='get_object',
            Params={'Bucket': bucket, 'Key': key},
            ExpiresIn=presign_expires,
        )
        return {"success": True, "url": url, "detail": r"Oh wow, look at you struggling! 🎉 Don't worry, we've lovingly compiled all your *totally unique* difficulties into a neat little HTML study guide — because clearly you needed the help. You're welcome! 😘 Check it out here before it expires and you're back to square one: {url}"}

    # Otherwise return constructed object URL (requires public access via ACL or bucket policy)
    try:
        loc = s3.get_bucket_location(Bucket=bucket).get('LocationConstraint')
    except ClientError:
        loc = None

    if loc is None:
        url = f"https://{bucket}.s3.amazonaws.com/{key}"
    else:
        url = f"https://{bucket}.s3.{loc}.amazonaws.com/{key}"

    return {"success": True, "url": url, "detail": "object_url_may_require_public_access"}


if __name__ == '__main__':
    import argparse
    import sys

    parser = argparse.ArgumentParser(description='Upload HTML from responses/ to S3 and return a URL')
    parser.add_argument('file', nargs='?', help='Path to HTML file (reads stdin if omitted) or a name inside responses/')
    parser.add_argument('--bucket', help='S3 bucket name (or set S3_BUCKET env)')
    parser.add_argument('--key', help='S3 object key (defaults to generated key)')
    parser.add_argument('--public', dest='public', action='store_true', help='Attempt to make uploaded object public-read')
    parser.add_argument('--no-public', dest='public', action='store_false', help='Do not set public ACL')
    parser.add_argument('--presign-if-private', dest='presign', action='store_true', help='Return a presigned GET URL if object is private')
    parser.set_defaults(public=True, presign=False)
    parser.add_argument('--no-dry-run', dest='dry', action='store_false', help='Actually upload to S3')
    args = parser.parse_args()

    responses_dir = os.environ.get('RESPONSES_DIR', 'responses')
    os.makedirs(responses_dir, exist_ok=True)

    # Determine the responses name to pass to the function
    if args.file:
        # if a path was provided and exists, copy it into responses/
        if os.path.exists(args.file):
            base = os.path.basename(args.file)
            dest = os.path.join(responses_dir, base)
            shutil.copyfile(args.file, dest)
            name = base
        else:
            # treat args.file as the name inside responses/
            name = args.file
    else:
        # read stdin, write into responses/<generated>.html
        html = sys.stdin.read()
        generated = f"generated-{int(time.time())}-{uuid.uuid4().hex}.html"
        dest = os.path.join(responses_dir, generated)
        with open(dest, 'w', encoding='utf-8') as fh:
            fh.write(html)
        name = generated

    if getattr(args, 'dry', True):
        print(json.dumps({"success": True, "url": f"file://{os.path.join(responses_dir, name)}", "detail": "dry_run_saved_local"}, indent=2))
    else:
        res = upload_html_and_get_object_url(name, bucket=args.bucket, key=args.key, make_public=args.public, presign_if_private=args.presign, responses_dir=responses_dir)
        print(json.dumps(res, indent=2))
