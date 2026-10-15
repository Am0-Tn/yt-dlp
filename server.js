import Fastify from 'fastify';
import YTDlpWrapPkg from 'yt-dlp-wrap';
import dotenv from 'dotenv';

dotenv.config();

// التصحيح لتوافق ESM مع CommonJS
const YTDlpWrap = YTDlpWrapPkg.default || YTDlpWrapPkg;

const fastify = Fastify({ logger: true });
const ytDlpWrap = new YTDlpWrap();

// مسار لجلب رابط البث المباشر
fastify.get('/api/resolve', async (request, reply) => {
  const { url, secret } = request.query;

  // التحقق من المفتاح السري للحماية
  if (process.env.API_SECRET_KEY && secret !== process.env.API_SECRET_KEY) {
    return reply.status(403).send({ error: 'غير مسموح بالوصول، المفتاح السري غير صحيح.' });
  }

  if (!url) {
    return reply.status(400).send({ error: 'الرجاء إرسال رابط الصفحة (url) كمتغير' });
  }

  try {
    // استخدام yt-dlp لجلب البيانات بصيغة JSON واستخراج رابط الفيديو
    const metadata = await ytDlpWrap.execPromise([
      url,
      '--dump-json',
      '--no-warnings'
    ]);

    const videoData = JSON.parse(metadata);
    
    // استخراج الرابط المباشر أو الـ formats المتاحة
    const directUrl = videoData.url || (videoData.formats && videoData.formats[videoData.formats.length - 1].url);

    return {
      success: true,
      title: videoData.title,
      directUrl: directUrl,
      formats: videoData.formats ? videoData.formats.map(f => ({ format_id: f.format_id, url: f.url, ext: f.ext })) : []
    };

  } catch (error) {
    request.log.error(error);
    return reply.status(500).send({ success: false, error: error.message });
  }
});

// تشغيل السيرفر
const start = async () => {
  try {
    await fastify.listen({ port: process.env.PORT || 3000, host: '0.0.0.0' });
    console.log(`Server is running on port ${process.env.PORT || 3000}`);
  } catch (err) {
    fastify.log.error(err);
    process.exit(1);
  }
};

start();
