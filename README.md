# 📸 私人相册服务

轻量级私人相册，Docker 一键部署，支持多相册库、标签管理、灯箱浏览。

## 🚀 一键部署

### 第一步：下载 `docker-compose.yml`

```yaml
services:
  photo-gallery:
    image: luojiutian007/photo-gallery:latest
    container_name: photo-gallery
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
      # 👇 改成你自己的照片目录
      - /path/to/your/photos:/photos:ro
    environment:
      - TZ=Asia/Shanghai
```

### 第二步：启动

```bash
docker compose up -d
```

### 第三步：打开

浏览器访问 **http://localhost:8080**

---

## 📁 多相册库

如果你有多个照片目录，挂载多个即可：

```yaml
volumes:
  - ./data:/app/data
  - /volume1/家庭照片:/photos:ro
  - /volume1/旅行:/travel:ro
  - /volume2/归档:/archive:ro
environment:
  - TZ=Asia/Shanghai
  - LIBRARIES='[{"name":"家庭照片","path":"/photos"},{"name":"旅行","path":"/travel"},{"name":"归档","path":"/archive"}]'
```

> **不设置 LIBRARIES 也可以**：默认会自动扫描 `/photos` 目录。只有多相册库时才需要设置。

---

## 🎯 功能一览

| 功能 | 说明 |
|------|------|
| 📁 多相册库 | 同时管理多个照片目录 |
| 🌳 目录树导航 | 左侧目录树快速跳转 |
| 🖼️ 灯箱浏览 | 图片/视频全屏查看，键盘切换 |
| 🏷️ 标签系统 | 文件级+目录级标签，主页标签云筛选 |
| ⭐ 收藏 & 评分 | 标记喜欢的照片 |
| 🔍 全局搜索 | 按文件名搜索，分页+无限滚动 |
| ✏️ 批量操作 | 多选 + 批量标签/收藏/评分 |
| 🎬 视频支持 | 自动生成封面帧，灯箱内播放 |
| 📱 响应式 | 手机/平板/电脑都能用 |

---

## ⌨️ 快捷键

| 按键 | 功能 |
|------|------|
| `←` `→` | 灯箱中切换上/下一张 |
| `Esc` | 关闭灯箱 |
| `T` | 打开标签筛选 |
| `F` | 灯箱中收藏 |
| `B` | 进入批量模式 |

---

## 🔧 配置说明

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TZ` | 时区 | `Asia/Shanghai` |
| `LIBRARIES` | 相册库配置（JSON数组） | 自动扫描 `/photos` |
| `PHOTO_ROOT` | 单相册路径（兼容旧版） | `/photos` |
| `PHOTO_ROOT_NAME` | 单相库名称 | `默认相册` |

### 数据持久化

所有数据存储在 `./data` 目录下：

```
data/
├── metadata.db    # SQLite 数据库（索引、标签、评分等）
├── library.json   # 相册库配置
└── cache/         # 缩略图缓存
```

> 删除 `data/` 目录会丢失所有标签、评分等元数据，但**不会影响原始照片**（照片以只读方式挂载）。

---

## 🐋 NAS 部署（群晖为例）

1. 打开 **Container Manager**
2. 点击 **项目** → **新增**
3. 填写项目名称，粘贴上面的 `docker-compose.yml`
4. 修改照片路径为 NAS 实际路径（如 `/volume1/photos`）
5. 点击 **完成**，等待拉取镜像并启动

---

## ❓ 常见问题

**Q: 为什么看不到照片？**
A: 检查 volumes 挂载路径是否正确，容器内的路径（冒号右边）需要和 LIBRARIES 里的 path 一致。

**Q: 支持什么格式的照片/视频？**
A: 图片：jpg/png/gif/webp/heic/bmp/tiff；视频：mp4/mov/avi/mkv/webm 等（需 ffmpeg 支持）。

**Q: 会修改我的原始照片吗？**
A: 不会。照片以只读方式（`:ro`）挂载，所有元数据（标签、评分等）存储在数据库中。

**Q: 怎么更新版本？**
A: `docker pull luojiutian007/photo-gallery:latest && docker compose up -d`
