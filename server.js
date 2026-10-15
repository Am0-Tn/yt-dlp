import Fastify from 'fastify';
import YTDlpWrapPkg from 'yt-dlp-wrap';
import * as cheerio from 'cheerio';
import dotenv from 'dotenv';

dotenv.config();

const YTDlpWrap = YTDlpWrapPkg.default || YTDlpWrapPkg;
const fastify = Fastify({ logger: true });
const ytDlpWrap = new YTDlpWrap();

/**
 * مسار البحث الدقيق وجلب الروابط بدقة للأنمي المطلوب
 * المتغيرات المطلوبة:
 * - animeName: اسم الأنمي (مثلاً "Demon Slayer" أو "Jujutsu Kaisen")
 * - episode: رقم الحلقة (مثلاً "1")
 * - secret: المفتاح السري للحماية
 */
fastify.get('/api/resolve-anime', async (request, reply) => {
  const { animeName, episode, secret } = request.query;

  // 1. التحقق من المفتاح السري للحماية
  if (process.env.API_SECRET_KEY && secret !== process.env.API_SECRET_KEY) {
    return reply.status(403).send({ 
      success: false, 
      error: 'غير مسموح بالوصول، المفتاح السري غير صحيح.' 
    });
  }

  // 2. التحقق من توفر بيانات البحث
  if (!animeName || !episode) {
    return reply.status(400).send({ 
      success: false, 
      error: 'الرجاء إرسال اسم الأنمي (animeName) ورقم الحلقة (episode).' 
    });
  }

  try {
    request.log.info(`Searching for: ${animeName} - Episode ${episode}`);

    // يمكنك اختيار موقعك المفضل المعتمد للبحث المباشر هنا
    // كمثال افتراضي، سنبحث عبر محرك بحث موجه أو موقع مخصص للأنمي
    const searchQuery = encodeURIComponent(`${animeName} الحلقة ${episode} مترجمة`);
    const searchUrl = `https://html.duckduckgo.com/html/?q=${searchQuery}`;

    // جلب نتائج البحث بدقة باستخدام Cheerio
    const searchResponse = await fetch(searchUrl, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
      }
    });
    
    const htmlText = await searchResponse.text();
    const $ = cheerio.load(htmlText);

    // استخراج أول رابط حقيقي مطابق للنتيجة من محرك البحث
    let targetLink = '';
    $('.result__url').each((i, el) => {
      const href = $(el).attr('href');
      if (href && !href.includes('duckduckgo')) {
        targetLink = href;
        return false; // الخروج من التكرار عند العثور على أول نتيجة مطابقة ودقيقة
      }
    });

    if (!targetLink) {
      // محاولة بديلة لاستخراج الروابط من نتائج البحث العادية
      const firstResult = $('.result__a').attr('href');
      if (firstResult) {
        targetLink = firstResult;
      }
    }

    if (!targetLink) {
      return reply.status(404).send({ 
        success: false, 
        error: `لم يتم العثور على مصدر دقيق للحلقة المطلوبة: ${animeName} - ${episode}` 
      });
    }

    request.log.info(`Found target page: ${targetLink}`);

    // 3. تمرير الرابط الدقيق المستخرج إلى yt-dlp لاستخراج رابط الفيديو الصافي (m3u8)
    const metadata = await ytDlpWrap.execPromise([
      targetLink,
      '--dump-json',
      '--no-warnings'
    ]);

    const videoData = JSON.parse(metadata);
    const directUrl = videoData.url || (videoData.formats && videoData.formats[videoData.formats.length - 1].url);

    return {
      success: true,
      anime: animeName,
      episode: episode,
      sourcePage: targetLink,
      title: videoData.title,
      directUrl: directUrl,
      formats: videoData.formats ? videoData.formats.map(f => ({ format_id: f.format_id, url: f.url, ext: f.ext })) : []
    };

  } catch (error) {
    request.log.error(error);
    return reply.status(500).send({ 
      success: false, 
      error: `حدث خطأ أثناء معالجة الطلب: ${error.message}` 
    });
  }
});

// تشغيل السيرفر
const start = async () => {
  try {
    await fastify.listen({ port: process.env.PORT || 3000, host: '0.0.0.0' });
    console.log(`Anime Resolver Server is running on port ${process.env.PORT || 3000}`);
  } catch (err) {
    fastify.log.error(err);
    process.exit(1);
  }
};

start();
