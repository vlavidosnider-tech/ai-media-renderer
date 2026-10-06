# AI Media Machine — FFmpeg Renderer

Free Render-compatible Python service used by n8n to assemble scene MP4s into one vertical master video.

## Endpoints

- GET /health
- POST /render

POST /render requires the X-Renderer-Key header.

Default output:
- 720x1280
- 30 FPS
- H.264
- AAC stereo
- source audio preserved
- subtitles burned at master level
