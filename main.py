import asyncio
import re
import time
import jmcomic
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from pathlib import Path
from tenacity import stop_after_attempt, wait_exponential, retry


class jmcomic_download(Star):
    MAX_RETRY_ATTEMPTS = 3
    WAIT_EXPONENTIAL_MAX = 10
    FILE_TIMEOUT = 30  # 秒
    ALBUM_ID_REGEX = r"^jm(\d+)$"
    PDF_SUFFIX = ".pdf"

    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        self.base_dir = Path(self.config.get("jm_download_dir", "/data/downloads")).resolve()
        self.pdf_dir = Path(self.config.get("jm_pdf_dir", "/data/pdf")).resolve()
        self.username = self.config.get("jm_username", "")
        self.password = self.config.get("jm_password", "")
        self._option_file = Path(__file__).parent / "option.yml"
        self._ensure_directories()
        logger.info("插件初始化完成")

    def _ensure_directories(self):
        """确保所有必要目录存在且有写入权限"""
        try:
            for directory in [self.base_dir, self.pdf_dir]:
                directory.mkdir(parents=True, exist_ok=True)
                (directory / ".permission_test").touch()
                logger.debug(f"目录权限验证通过: {directory}")
        except PermissionError:
            logger.critical(f"目录权限不足: {directory}")
            raise
        except Exception as e:
            logger.error(f"目录初始化失败: {str(e)}")
            raise

    def _create_option_file(self):
        """生成JMComic的配置文件"""
        if not self._option_file.exists():
            content = f"""log: true
client:
  impl: api
  retry_times: 3
download:
  cache: true
  image:
    decode: true
    suffix: .jpg
  threading:
    image: 30
    photo: 8
dir_rule:
  base_dir: {self.base_dir}
  rule: Bd_Aid_Pindex
plugins:
  after_init:
    - plugin: login
      kwargs:
          username: {self.username}
          password: {self.password}
  after_album:
    - plugin: img2pdf
      kwargs:
        pdf_dir: {self.pdf_dir}
        filename_rule: Aid"""
            self._option_file.write_text(content)
            logger.info("选项文件创建成功")

    @retry(stop=stop_after_attempt(MAX_RETRY_ATTEMPTS),
           wait=wait_exponential(multiplier=1, max=WAIT_EXPONENTIAL_MAX))
    async def _download_album(self, album_id: str) -> Path:
        """下载专辑并返回PDF路径"""
        logger.info(f"开始下载: jm{album_id}")
        self._create_option_file()

        # 生成预期PDF路径
        expected_pdf = self.pdf_dir / f"{album_id}{self.PDF_SUFFIX}"

        # 检查是否已有缓存
        if expected_pdf.exists():
            logger.info(f"使用缓存文件: {expected_pdf}")
            return expected_pdf

        # 启动下载（同步函数放入线程池执行）
        await asyncio.to_thread(
            jmcomic.download_album,
            f"jm{album_id}",
            jmcomic.create_option_by_file(str(self._option_file))
        )

        # 等待文件生成（带超时）
        start_time = time.time()
        while not expected_pdf.exists():
            if time.time() - start_time > self.FILE_TIMEOUT:
                raise TimeoutError(f"文件生成超时: {expected_pdf}")
            await asyncio.sleep(1)

        logger.info(f"下载完成: {expected_pdf}")
        return expected_pdf

    def _validate_album_id(self, album_id: str) -> bool:
        """验证专辑ID格式有效性"""
        return bool(re.match(self.ALBUM_ID_REGEX, f"jm{album_id}")) and len(album_id) <= 8

    @filter.regex(ALBUM_ID_REGEX, flags=re.IGNORECASE)
    async def handle_album_id(self, event: AstrMessageEvent):
        """处理用户输入的专辑ID"""
        album_id = event.get_messages()[0]
        album_id = str(album_id)
        if not self._validate_album_id(album_id):
            yield event.plain_result("请输入有效的本子ID，例如: jm123456")
            return

        try:
            # 发送确认消息
            yield event.plain_result(f"开始处理 jm{album_id}，请稍候...")

            # 执行下载
            pdf_file = await self._download_album(album_id)
            pdf_file = pdf_file.name
            logger.info(f"PDF文件保存在: {pdf_file}")

            # 发送文件
            chain = [
                Comp.Plain(f"处理完成：jm{album_id}"),
                Comp.File(file=pdf_file, name=f"jm{album_id}{self.PDF_SUFFIX}"),
            ]
            yield event.chain_result(chain)

        except TimeoutError as e:
            logger.error(str(e))
            yield event.plain_result("⚠️ 文件生成超时，请稍后重试")
        except Exception as e:
            logger.exception(f"处理失败: jm{album_id}")
            yield event.plain_result(f"❌ 处理失败: {str(e)}")