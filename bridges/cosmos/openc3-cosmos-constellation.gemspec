# encoding: ascii-8bit
spec = Gem::Specification.new do |s|
  s.name = 'openc3-cosmos-constellation'
  s.summary = 'Constellation workflow forecasts in OpenC3 COSMOS'
  s.description = <<-EOF
    A CONSTELLATION target whose packets a bridge microservice fills from the Constellation
    feed: the next predicted pass, the next 24 hours, the soonest rain-fade swap to decide, a
    day's capacity across the antennas (CAPACITY), the next pass's MODCOD plan (MODCOD), and the
    run they came from. Every value is a forecast.
  EOF
  s.authors = ['Constellation Space Corp']
  s.email = ['support@constellation.space']
  s.homepage = 'https://constellation.space'
  s.metadata = { 'source_code_uri' => 'https://github.com/constellationhq/connectors' }
  s.platform = Gem::Platform::RUBY
  s.version = ENV['VERSION'] || '0.1.0'
  s.licenses = ['Apache-2.0']
  s.files = Dir.glob('{targets,lib,public,tools,microservices}/**/*').reject { |f| f.include?('__pycache__') } + %w(Rakefile plugin.txt)
end
