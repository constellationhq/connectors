# encoding: ascii-8bit
spec = Gem::Specification.new do |s|
  s.name = 'openc3-cosmos-constellation'
  s.summary = 'Constellation workflow forecasts in OpenC3 COSMOS'
  s.description = <<~EOF.tr("\n", ' ').strip
    A CONSTELLATION target whose packets a bridge microservice fills from the Constellation
    feed: the next predicted pass, the next 24 hours, the soonest rain-fade swap to decide, a
    day's capacity across the antennas (CAPACITY), the next pass's MODCOD plan (MODCOD), and the
    run they came from. A Telemetry Viewer screen shows them all, and a Constellation Forecast tool
    charts each workflow. Every value is a forecast.
  EOF
  s.authors = ['Constellation Space Corp']
  s.email = ['support@constellation.space']
  s.homepage = 'https://platform.constellation.space/docs/connectors/cosmos'
  s.metadata = {
    'source_code_uri' => 'https://github.com/constellationhq/connectors',
    'openc3_store_title' => 'Constellation Forecasts',
    'openc3_store_keywords' => 'forecast, ground station, pass prediction, rain fade, modcod, antenna capacity, scheduling',
    'openc3_store_image' => 'public/store_img.png',
    'openc3_store_access_type' => 'public',
    # VARIABLE_DESCRIPTION in plugin.txt is the newest keyword the plugin uses.
    'openc3_cosmos_minimum_version' => '7.0.0',
  }
  s.platform = Gem::Platform::RUBY
  s.version = ENV['VERSION'] || '0.1.0'
  s.licenses = ['Apache-2.0']
  s.files = Dir.glob('{targets,lib,public,tools,microservices}/**/*').reject { |f| f.include?('__pycache__') } + %w(Rakefile plugin.txt LICENSE)
end
