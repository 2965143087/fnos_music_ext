# 预置离线 wheel 目录
#
# 把目标平台（linux x86_64 / cp313）的 .whl 放进本目录后打包，
# 镜像构建时 pip 会优先使用这些 wheel，整段依赖安装无需联网。
#
# 生成方式（在 Linux x86_64 + Python 3.13 上执行）：
#   python3 -m pip download --only-binary=:all: \
#     -i https://pypi.tuna.tsinghua.edu.cn/simple \
#     -r qqmusic-service/requirements.txt -d container/wheels
#
# .whl 文件不纳入版本控制（见 .gitignore），仅随 fpk 分发。
