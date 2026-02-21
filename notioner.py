"""One-shot helper: upload HTML to S3 and return a URL users can open.

This file exposes a single function `upload_html_and_get_object_url` which:
- takes an HTML string
- uploads it to the provided S3 bucket
- attempts to make the object publicly readable (if requested)
- falls back to returning a presigned URL when ACLs are disallowed or bucket is private

The function returns a dict: {"success": bool, "url": str, "detail": str}.

Usage:
	from notioner import upload_html_and_get_object_url
	res = upload_html_and_get_object_url(html, bucket='my-bucket')

CLI:
	python notioner.py myfile.html --bucket my-bucket --no-dry-run --public
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Optional, Dict, Any

import boto3
from botocore.exceptions import ClientError

def upload_html_and_get_object_url(
	html: str,
	bucket: Optional[str] = None,
	key: Optional[str] = None,
	make_public: bool = True,
	presign_if_private: bool = True,
	presign_expires: int = 3600,
	aws_access_key_id: Optional[str] = None,
	aws_secret_access_key: Optional[str] = None,
	aws_region: Optional[str] = None,
) -> Dict[str, Any]:
	"""Upload an HTML string to S3 and return a URL that users can open.

	Behavior:
	- If make_public=True, the function will try to upload the object with public-read ACL.
	  If the bucket disallows ACLs, it will upload without ACL and (if presign_if_private)
	  return a presigned GET URL so users can open the file.
	- If make_public=False and presign_if_private=True, the function uploads privately and
	  returns a presigned URL.
	- The returned URL is either an object URL (https://bucket.s3.region.amazonaws.com/key)
	  or a presigned URL if the object is private or ACLs couldn't be set.
	"""
	# load .env if available (keeps imports side-effect free until function call)
	try:
		from dotenv import load_dotenv

		load_dotenv()
	except Exception:
		pass

	bucket = bucket or os.environ.get('S3_BUCKET')
	if not bucket:
		return {"success": False, "error": "S3 bucket not provided (arg or S3_BUCKET env)"}
	# construct object key if not provided
	if key:
		key = key if key.lower().endswith('.html') else key + '.html'
	else:
		key = f"panikbot/{int(time.time())}-{uuid.uuid4().hex}.html"

	# resolve credentials
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

	# Try upload (may raise ClientError)
	try:
		s3.put_object(Bucket=bucket, Key=key, Body=html.encode('utf-8'), **extra_args)
	except ClientError as e:
		code = getattr(e, 'response', {}).get('Error', {}).get('Code')
		# ACLs not supported: upload without ACL and return presigned URL if requested
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
				# otherwise return the object URL (may be private)
				# fall through to construct object URL below
			except ClientError as e2:
				return {"success": False, "error": str(e2), "code": getattr(e2, 'response', {}).get('Error', {}).get('Code')}
		else:
			return {"success": False, "error": str(e), "code": code}

	# Upload succeeded; return object URL if possible
	# If user requested presign (e.g., make_public False with presign_if_private True), return presigned URL
	if not make_public and presign_if_private:
		url = s3.generate_presigned_url(
			ClientMethod='get_object',
			Params={'Bucket': bucket, 'Key': key},
			ExpiresIn=presign_expires,
		)
		return {"success": True, "url": url, "text": f"I've processed your thoughts and identified your most difficult points. Based on our conversations, I've written a personalised study guide to help you tackle them. You can view it here: {url}"}

	# Build standard S3 object URL (works if object is public via ACL or bucket policy)
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
	import json

	parser = argparse.ArgumentParser(description='Upload HTML to S3 and return an accessible URL')
	parser.add_argument('file', nargs='?', help='Path to HTML file (reads stdin if omitted)')
	parser.add_argument('--bucket', help='S3 bucket name (or set S3_BUCKET env)')
	parser.add_argument('--key', help='S3 object key (defaults to generated key)')
	parser.add_argument('--public', dest='public', action='store_true', help='Attempt to make uploaded object public-read')
	parser.add_argument('--no-public', dest='public', action='store_false', help='Do not set public ACL')
	parser.add_argument('--presign-if-private', dest='presign', action='store_true', help='Return a presigned GET URL if object is private')
	parser.set_defaults(public=True, presign=False)
	parser.add_argument('--no-dry-run', dest='dry', action='store_false', help='Actually upload to S3')
	args = parser.parse_args()

	if args.file:
		with open(args.file, 'r', encoding='utf-8') as f:
			html = f.read()
	else:
		html = sys.stdin.read()

	# if dry run, just write locally
	if getattr(args, 'dry', True):
		import tempfile

		fd, path = tempfile.mkstemp(suffix='.html', prefix='panikbot-')
		with os.fdopen(fd, 'w', encoding='utf-8') as f:
			f.write(html)
		print(json.dumps({"success": True, "url": f"file://{path}", "detail": "dry_run_saved_local"}, indent=2))
	else:
		res = upload_html_and_get_object_url(html, bucket=args.bucket, key=args.key, make_public=args.public, presign_if_private=args.presign)
		print(json.dumps(res, indent=2))
