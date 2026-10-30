import json
import urllib.request

for num in (16, 17, 18):
    url = f"https://gitee.com/api/v5/repos/Sugaria/TaskLoom/pulls/{num}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        pr = json.loads(urllib.request.urlopen(req).read().decode())
        print("=" * 60)
        print(f"PR #{pr['number']}: {pr['title']}")
        print(f"Author: {pr['user']['name']} ({pr['user']['login']})")
        print(f"Base: {pr['base']['ref']} | Head: {pr['head']['ref']} ({pr['head']['sha'][:7]})")
        print(f"Created: {pr['created_at']}")
        print("Body:\n" + pr["body"].strip())
    except Exception as e:
        print(f"Error loading PR #{num}: {e}")
