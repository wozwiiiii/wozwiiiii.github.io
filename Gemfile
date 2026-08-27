source "https://mirrors.tuna.tsinghua.edu.cn/rubygems/"

gemspec

# Ruby 3.4 将 bigdecimal 从标准库移除，Jekyll 依赖的 liquid 需要
gem "bigdecimal"
gem "csv"
gem "base64"

# _config.yml plugins 声明的插件
group :jekyll_plugins do
  gem "jekyll-paginate"
  gem "jekyll-seo-tag"
  gem "jekyll-feed"
  gem "jekyll-avatar"
  gem "jekyll-optional-front-matter"
  gem "jekyll-readme-index"
  gem "jekyll-relative-links"
  gem "jekyll-titles-from-headings"
end
