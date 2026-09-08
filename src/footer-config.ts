import { loadRelayConfig, type RelayConfig } from "./config.js";

export interface FooterConfig extends RelayConfig {
  pluginInstallationId: string;
  pluginSigningSecret: string;
  replyRegistrationToken: string;
  slackReplyToken: string;
  slackReadToken: string;
  slackReplyActor: "user" | "bot";
  footerConsumerUrl: string;
}

export function loadFooterConfig(env: NodeJS.ProcessEnv): FooterConfig {
  const base = loadRelayConfig(env);
  if (!base.footerEnabled) throw new Error("footer_disabled");
  const required = (key: string) => {
    const value = env[key]?.trim();
    if (!value) throw new Error("footer_not_configured");
    return value;
  };
  const pluginSigningSecret = required("MULTICA_PLUGIN_SIGNING_SECRET");
  if (!/^whsec_[a-f0-9]{64}$/iu.test(pluginSigningSecret))
    throw new Error("footer_not_configured");
  const replyRegistrationToken = required("RELAY_REPLY_TOKEN");
  if (replyRegistrationToken.length < 32)
    throw new Error("footer_not_configured");
  const slackReplyActor = required("SLACK_REPLY_ACTOR");
  if (slackReplyActor !== "user" && slackReplyActor !== "bot")
    throw new Error("footer_not_configured");
  const footerConsumerUrl = required("RELAY_FOOTER_CONSUMER_URL");
  const url = new URL(footerConsumerUrl);
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.hash ||
    url.search
  )
    throw new Error("footer_not_configured");
  return {
    ...base,
    pluginInstallationId: required("MULTICA_PLUGIN_INSTALLATION_ID"),
    pluginSigningSecret,
    replyRegistrationToken,
    slackReplyActor,
    slackReplyToken: required("SLACK_REPLY_TOKEN"),
    slackReadToken:
      env.SLACK_READ_TOKEN?.trim() || required("SLACK_REPLY_TOKEN"),
    footerConsumerUrl,
  };
}
