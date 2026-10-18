#!/usr/bin/env node
// No target-site requests, profiles, cookies or secrets are used by this probe.
import path from 'node:path';
import {createRequire} from 'node:module';
import {parseArgs} from 'node:util';

const {values}=parseArgs({options:{project:{type:'string',default:process.cwd()},channel:{type:'string',default:'chrome'},headed:{type:'boolean',default:false},help:{type:'boolean',default:false}},allowPositionals:false});
if(values.help){
  console.log('Usage: node inspect-browser.mjs --project <directory-with-playwright> [--channel chrome] [--headed]\nCompares isolated Chrome request/runtime identities using locally fulfilled pages. No target-site requests.');
  process.exit(0);
}
const requireFromProject=createRequire(path.join(path.resolve(values.project),'package.json'));
let chromium;
try {({chromium}=requireFromProject('playwright'));}
catch {console.error('Playwright is not available in the specified project. Use a project that already has it installed.');process.exit(1);}

const allowedHeaders=new Set(['user-agent','accept','accept-language','sec-ch-ua','sec-ch-ua-mobile','sec-ch-ua-platform']);
const results=[];
for(const headless of values.headed?[true,false]:[true]){
  let browser;
  try {
    browser=await chromium.launch({channel:values.channel,headless,timeout:15000});
    const session=await browser.newBrowserCDPSession();
    let userAgent;
    try {({userAgent}=await session.send('Browser.getVersion'));}
    finally {await session.detach();}
    if(typeof userAgent!=='string' || !/\b(?:Headless)?Chrome\/\d+\./.test(userAgent))throw Error('Unexpected browser identity; no UA was fabricated.');
    const modes=headless?['default','standard-chrome-ua']:['default'];
    for(const mode of modes){
      const context=await browser.newContext({locale:'en-US',viewport:{width:1280,height:800},serviceWorkers:'block',...(mode==='standard-chrome-ua'?{userAgent:userAgent.replace(/\bHeadlessChrome\//g,'Chrome/')}:{} )});
      try {
        let headers;
        await context.route('**/*',async route=>{
          headers=await route.request().allHeaders();
          await route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><title>Local crawler identity probe</title>'});
        });
        const page=await context.newPage();
        await page.goto('https://crawler-diagnostic.invalid/',{waitUntil:'domcontentloaded',timeout:10000});
        const runtime=await page.evaluate(()=>({userAgent:navigator.userAgent,language:navigator.language,webdriver:navigator.webdriver,userAgentData:navigator.userAgentData?.toJSON()}));
        results.push({headless,mode,browserVersion:browser.version(),headers:Object.fromEntries(Object.entries(headers).filter(([key])=>allowedHeaders.has(key))),runtime});
      }finally {await context.close();}
    }
  }finally {await browser?.close();}
}
console.log(JSON.stringify({scope:'local-intercepted-pages-only',targetSiteVerified:false,observations:results},null,2));
