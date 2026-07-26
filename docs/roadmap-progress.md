# MikeVideo YouTube-parity roadmap — autonomous build progress

**This file is the source of truth for the 30-min build loop.** Each run: finish any `IN_PROGRESS`
phase, else start the first `PENDING` one; implement it end-to-end (cloud + web + app where relevant),
**deploy**, **test (chrome-pool for web, adb for the app)**, commit both repos, then mark it `DONE`
(or `FAILED: <reason>`). One phase per run. Never touch other phases. Don't disturb the OSM stack.

Status legend: `PENDING` · `IN_PROGRESS` · `DONE` · `FAILED`

| Phase | Status | Scope (build across cloud + web + app; test all touched surfaces) |
|---|---|---|
| **P1** | IN_PROGRESS | **Engagement primitives.** Migration: `views`(count via a `video_views` event or a counter col), `likes`(video_id,user_id unique), `watch_progress`(video_id,user_id,position_sec,updated). Endpoints: `POST /api/videos/{id}/view`, `POST/DELETE /api/videos/{id}/like`, `GET/PUT /api/videos/{id}/progress`; add `views/likes/liked/progress` to video detail+list. Web: show view count + age on cards, like button + count on watch, **resume** playback from saved position + a "Continue watching" row. App: like button, resume, report views/progress. |
| **P2** | PENDING | **Playlists + Watch Later / Save.** Migration: `playlists`, `playlist_items`. CRUD endpoints + a built-in "Watch Later". Web: Save-to-playlist menu on cards/watch, a Playlists page, Watch Later. App: save menu + playlists. |
| **P3** | PENDING | **Channels (@handle) + public profile.** Migration: `channels`(user_id, handle unique, display_name, avatar, bio) or reuse user; `GET /api/channels/{handle}` (public: their public videos + counts). Web: a channel page (avatar, name, subscriber count, video grid), channel name+avatar on cards linking to it. App: channel view. |
| **P4** | PENDING | **Subscriptions.** Migration: `subscriptions`(subscriber_user_id, channel_user_id). Subscribe/unsubscribe endpoints + `GET /api/feed/subscriptions`. Web: Subscribe button on channel/watch, a left sidebar (Home/Shorts/Subscriptions/Library), a Subscriptions feed. App: subscribe + subs feed. |
| **P5** | PENDING | **Comments.** Migration: `comments`(video_id,user_id,parent_id,text,hearts,likes,created). Threaded CRUD + like/heart. Web: comments section on watch (post, reply, like, heart-by-owner, sorted). App: comments UI. |
| **P6** | PENDING | **Personalized home feed / recommendations.** `GET /api/feed/home`: blend your subs' recent + popular (views/likes) + tag-affinity from your watch history/likes; fall back to public. Web: home becomes the recommended feed for signed-in users. App: recommended home. |
| **P7** | PENDING | **Related / up-next + Autoplay.** `GET /api/videos/{id}/related` (tag/transcript similarity + popularity). Web: replace the library rail with real related + an autoplay-next toggle. App: related + autoplay. |
| **P8** | PENDING | **Notifications.** Migration: `notifications`(user_id,type,payload,read,created). Fan-out on new upload to subscribers. `GET /api/notifications` + mark-read. Web: a bell with unread count + dropdown. App: notifications (+ MikeOS notification/hive if wired). |
| **P9** | PENDING | **Shorts.** `GET /api/shorts` (portrait, short-duration public videos). Web: a vertical full-screen swipe feed. App: a Shorts swipe pager. (Portrait encoding already correct.) |
| **P10** | PENDING | **Player polish.** Web: quality selector (HLS levels via hls.js), playback-speed menu, miniplayer (keep playing while browsing), theater mode, keyboard shortcuts. App: speed + quality + PiP/background. |
| **P11** | PENDING | **Trending / Explore.** `GET /api/explore` + `?tag=`: popular public videos, categories from AI tags. Web: an Explore page with tag chips. App: explore. |
| **P12** | PENDING | **Creator Studio (analytics).** `GET /api/studio/overview` + per-video stats: views over time, watch-time (from progress), likes, top videos. Web: a Studio dashboard. App: a lightweight studio view. |
| **P13** | PENDING | **Custom thumbnails + light edit.** Pick a frame or upload a thumbnail (`POST /api/videos/{id}/thumbnail`); optional trim (re-encode a subclip). Web: thumbnail picker on watch/studio. App: thumbnail picker. |
| **P14** | PENDING | **Embeds + external share.** An embeddable player (`GET /embed/{id}` for public videos) + oEmbed (`GET /api/oembed`). Web: a Share→Embed dialog with copyable iframe. (App: n/a.) |

## Run log (append one line per run)
- (empty)
