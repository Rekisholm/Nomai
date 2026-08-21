# GitLab CVE-2016-9086

GitLab is a Git project management platform written in Ruby. The export/import project feature added after version 8.9 did not handle symbolic links in archives correctly, allowing authenticated users to read arbitrary files on the server.

## Startup and Access

```bash
docker compose up -d
```

After the environment starts, visit `http://your-ip:8080` to open the GitLab home page.

Default administrator credentials:
`root/vulhub123456`

## Reference

https://github.com/vulhub/vulhub/blob/master/gitlab/CVE-2016-9086/README.zh-cn.md
