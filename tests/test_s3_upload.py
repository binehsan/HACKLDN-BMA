from notioner import upload_html_to_s3


def test_dry_run_saves_local_file():
    html = '<html><body><h1>Hello</h1></body></html>'
    res = upload_html_to_s3(html, dry_run=True)
    assert res["success"] is True
    assert res["url"].startswith("file://")
