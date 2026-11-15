/* SPDX-License-Identifier: GPL-2.0-only
 * Wi-Fi infrastructure experiment for ns-3.35.
 * Uses the AP/STA construction pattern from ns-3 examples/tutorial/third.cc.
 * See README.md for measurement definitions and limitations.
 */
#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/mobility-module.h"
#include "ns3/wifi-module.h"
#include "ns3/applications-module.h"
#include "ns3/flow-monitor-module.h"
#include <fstream>
#include <iomanip>
#include <sstream>
#include <vector>
#include <cmath>

using namespace ns3;

struct AppCount
{
  uint64_t txBytes = 0, rxBytes = 0, rxActive = 0;
};
static std::vector<AppCount> counts;
static double sendStart, sendStop;

// OnOff Tx fires only when Socket::Send accepted the complete write.
// TCP acceptance by a socket is NOT delivery to the remote application.
static void AppTx (uint32_t i, Ptr<const Packet> p)
{
  counts.at (i).txBytes += p->GetSize ();
}
static void AppRx (uint32_t i, Ptr<const Packet> p, const Address &)
{
  counts.at (i).rxBytes += p->GetSize ();
  double t = Simulator::Now ().GetSeconds ();
  if (t >= sendStart && t < sendStop)
    counts.at (i).rxActive += p->GetSize ();
}
static std::string NumberOrBlank (double numerator, uint64_t denominator)
{
  if (!denominator) return "";
  std::ostringstream s;
  s << std::setprecision (12) << numerator / denominator;
  return s.str ();
}
static void OpenOutput (std::ofstream &f, const std::string &path)
{
  f.open (path.c_str ());
  NS_ABORT_MSG_IF (!f, "Cannot write " << path << "; create its parent directory first.");
  f << std::setprecision (12);
}
struct IpCount
{
  uint64_t tx = 0, rx = 0, txBytes = 0, rxBytes = 0, lostMonitor = 0;
  double delay = 0;
};

int main (int argc, char *argv[])
{
  uint32_t stations = 5, seed = 12345, packetSize = 1024;
  uint64_t run = 1;
  double duration = 30, cleanup = 10, start = 3, halfSide = 25, speed = 1.5;
  double rateMbps = 1.0, txPower = 16;
  std::string transport = "udp", mobility = "moving", output = "wifi-result";
  bool xml = false, pcap = false;
  CommandLine cmd (__FILE__);
  cmd.AddValue ("stations", "Number of sending STAs, excluding AP (1..200)", stations);
  cmd.AddValue ("transport", "udp or tcp (TCP NewReno)", transport);
  cmd.AddValue ("seed", "RNG seed", seed);
  cmd.AddValue ("run", "Independent RNG run number", run);
  cmd.AddValue ("duration", "Common application sending window, seconds", duration);
  cmd.AddValue ("cleanup", "Drain interval after sources stop, seconds", cleanup);
  cmd.AddValue ("start", "Common source start time, seconds", start);
  cmd.AddValue ("rateMbps", "Requested application rate per station, decimal Mbps", rateMbps);
  cmd.AddValue ("packetSize", "OnOff application write size, bytes", packetSize);
  cmd.AddValue ("halfSide", "Area is [-halfSide,+halfSide]^2 metres", halfSide);
  cmd.AddValue ("speed", "RandomWalk speed, m/s", speed);
  cmd.AddValue ("mobility", "moving or static (static is diagnostic baseline)", mobility);
  cmd.AddValue ("txPower", "Transmit power, dBm", txPower);
  cmd.AddValue ("output", "Output path prefix; parent directory must exist", output);
  cmd.AddValue ("xml", "Also save raw FlowMonitor XML", xml);
  cmd.AddValue ("pcap", "Also save AP pcap for diagnostics", pcap);
  cmd.Parse (argc, argv);
  NS_ABORT_MSG_IF (stations < 1 || stations > 200, "stations must be 1..200");
  NS_ABORT_MSG_IF (transport != "tcp" && transport != "udp", "transport must be tcp/udp");
  NS_ABORT_MSG_IF (mobility != "moving" && mobility != "static", "mobility must be moving/static");
  NS_ABORT_MSG_IF (!seed || !run, "seed and run must be positive");
  NS_ABORT_MSG_IF (!std::isfinite(duration) || duration <= 0 ||
                   !std::isfinite(cleanup) || cleanup < 0 ||
                   !std::isfinite(start) || start < 1 ||
                   !std::isfinite(rateMbps) || rateMbps <= 0 || rateMbps > 100 ||
                   !std::isfinite(halfSide) || halfSide <= 0 ||
                   !std::isfinite(speed) || speed <= 0 ||
                   !std::isfinite(txPower), "Invalid numeric parameter");
  NS_ABORT_MSG_IF (packetSize < 64 || packetSize > 1400, "Use packetSize 64..1400 to avoid IPv4 fragmentation");
  sendStart = start;
  sendStop = start + duration;
  const double simStop = sendStop + cleanup;
  RngSeedManager::SetSeed (seed);
  RngSeedManager::SetRun (run);
  Config::SetDefault ("ns3::TcpL4Protocol::SocketType", TypeIdValue (TcpNewReno::GetTypeId ()));
  Config::SetDefault ("ns3::TcpSocket::SegmentSize", UintegerValue (packetSize));

  NodeContainer sta, ap;
  sta.Create (stations);
  ap.Create (1);
  NodeContainer all;
  all.Add (sta);
  all.Add (ap);

  // Explicit, reproducible channel: 802.11a / 20 MHz / fixed PHY rate.
  YansWifiChannelHelper channel;
  channel.SetPropagationDelay ("ns3::ConstantSpeedPropagationDelayModel");
  channel.AddPropagationLoss ("ns3::LogDistancePropagationLossModel",
      "Exponent", DoubleValue (3.0), "ReferenceDistance", DoubleValue (1.0),
      "ReferenceLoss", DoubleValue (46.6777));
  YansWifiPhyHelper phy;
  phy.SetChannel (channel.Create ());
  phy.Set ("TxPowerStart", DoubleValue (txPower));
  phy.Set ("TxPowerEnd", DoubleValue (txPower));
  WifiHelper wifi;
  wifi.SetStandard (WIFI_STANDARD_80211a);
  wifi.SetRemoteStationManager ("ns3::ConstantRateWifiManager",
      "DataMode", StringValue ("OfdmRate24Mbps"),
      "ControlMode", StringValue ("OfdmRate6Mbps"),
      "RtsCtsThreshold", UintegerValue (65535));
  WifiMacHelper mac;
  Ssid ssid ("wifi-transport-study");
  mac.SetType ("ns3::StaWifiMac", "Ssid", SsidValue (ssid),
               "ActiveProbing", BooleanValue (false), "QosSupported", BooleanValue (false));
  NetDeviceContainer staDev = wifi.Install (phy, mac, sta);
  mac.SetType ("ns3::ApWifiMac", "Ssid", SsidValue (ssid),
               "QosSupported", BooleanValue (false));
  NetDeviceContainer apDev = wifi.Install (phy, mac, ap);
  NetDeviceContainer devices;
  devices.Add (staDev);
  devices.Add (apDev);
  wifi.AssignStreams (devices, 10000);

  // Stream ranges separated from Wi-Fi/TCP: TCP/UDP use identical mobility
  // for a given station count, seed and run. Initial points are strictly inside bounds.
  Ptr<UniformRandomVariable> pos = CreateObject<UniformRandomVariable> ();
  pos->SetStream (1);
  Ptr<ListPositionAllocator> positions = CreateObject<ListPositionAllocator> ();
  std::vector<Vector> initial;
  for (uint32_t i = 0; i < stations; ++i)
    {
      Vector v (pos->GetValue (-halfSide * 0.99, halfSide * 0.99),
                pos->GetValue (-halfSide * 0.99, halfSide * 0.99), 0);
      initial.push_back (v);
      positions->Add (v);
    }
  MobilityHelper move;
  move.SetPositionAllocator (positions);
  std::ostringstream speedRv;
  speedRv << "ns3::ConstantRandomVariable[Constant=" << speed << "]";
  if (mobility == "moving")
    move.SetMobilityModel ("ns3::RandomWalk2dMobilityModel",
        "Mode", StringValue ("Time"), "Time", TimeValue (Seconds (1)),
        "Speed", StringValue (speedRv.str ()),
        "Bounds", RectangleValue (Rectangle (-halfSide, halfSide, -halfSide, halfSide)));
  else
    move.SetMobilityModel ("ns3::ConstantPositionMobilityModel");
  move.Install (sta);
  move.AssignStreams (sta, 100);
  MobilityHelper fixed;
  fixed.SetMobilityModel ("ns3::ConstantPositionMobilityModel");
  fixed.Install (ap);
  ap.Get (0)->GetObject<MobilityModel> ()->SetPosition (Vector (0, 0, 0));

  InternetStackHelper internet;
  internet.Install (all);
  Ipv4AddressHelper address;
  address.SetBase ("10.1.1.0", "255.255.255.0");
  Ipv4InterfaceContainer staIp = address.Assign (staDev);
  Ipv4InterfaceContainer apIp = address.Assign (apDev);
  // Same subnet, direct STA -> AP delivery; no multi-hop routing experiment.
  const std::string factory = transport == "tcp" ? "ns3::TcpSocketFactory" : "ns3::UdpSocketFactory";
  counts.resize (stations);
  for (uint32_t i = 0; i < stations; ++i)
    {
      uint16_t port = 9000 + i;
      PacketSinkHelper sink (factory, InetSocketAddress (Ipv4Address::GetAny (), port));
      ApplicationContainer receivers = sink.Install (ap.Get (0));
      receivers.Start (Seconds (0));
      // Keep sinks running until Simulator::Destroy, including the drain period.
      receivers.Get (0)->TraceConnectWithoutContext ("Rx", MakeBoundCallback (&AppRx, i));
      OnOffHelper source (factory, InetSocketAddress (apIp.GetAddress (0), port));
      source.SetAttribute ("DataRate", DataRateValue (DataRate (static_cast<uint64_t> (rateMbps * 1e6))));
      source.SetAttribute ("PacketSize", UintegerValue (packetSize));
      source.SetAttribute ("OnTime", StringValue ("ns3::ConstantRandomVariable[Constant=1]"));
      source.SetAttribute ("OffTime", StringValue ("ns3::ConstantRandomVariable[Constant=0]"));
      ApplicationContainer senders = source.Install (sta.Get (i));
      source.AssignStreams (sta.Get (i), 20000 + 2 * i);
      senders.Start (Seconds (sendStart));
      senders.Stop (Seconds (sendStop));
      senders.Get (0)->TraceConnectWithoutContext ("Tx", MakeBoundCallback (&AppTx, i));
    }
  if (pcap)
    {
      phy.SetPcapDataLinkType (WifiPhyHelper::DLT_IEEE802_11_RADIO);
      phy.EnablePcap (output, apDev.Get (0));
    }
  FlowMonitorHelper flowHelper;
  Ptr<FlowMonitor> monitor = flowHelper.InstallAll ();
  Simulator::Stop (Seconds (simStop));
  Simulator::Run ();
  monitor->CheckForLostPackets ();
  Ptr<Ipv4FlowClassifier> classifier = DynamicCast<Ipv4FlowClassifier> (flowHelper.GetClassifier ());
  std::vector<IpCount> perStation (stations);
  for (const auto &entry : monitor->GetFlowStats ())
    {
      Ipv4FlowClassifier::FiveTuple t = classifier->FindFlow (entry.first);
      if (t.protocol != (transport == "tcp" ? 6 : 17) ||
          t.destinationAddress != apIp.GetAddress (0) ||
          t.destinationPort < 9000 || t.destinationPort >= 9000 + stations) continue;
      uint32_t i = t.destinationPort - 9000;
      if (t.sourceAddress != staIp.GetAddress (i)) continue;
      const auto &s = entry.second;
      auto &p = perStation.at (i);
      p.tx += s.txPackets; p.rx += s.rxPackets;
      p.txBytes += s.txBytes; p.rxBytes += s.rxBytes;
      p.lostMonitor += s.lostPackets; p.delay += s.delaySum.GetSeconds ();
    }
  std::ofstream flows, summary, positionFile;
  OpenOutput (flows, output + "-flows.csv");
  OpenOutput (summary, output + "-summary.csv");
  OpenOutput (positionFile, output + "-positions.csv");
  const std::string metrics = "tx_ip_packets,rx_ip_packets,unreceived_ip_packets,pdr_ip_pct,loss_ip_pct,delay_ip_ms,rx_ip_bytes,throughput_ip_kbps,app_tx_accepted_bytes,app_rx_bytes,app_rx_active_bytes,goodput_active_kbps,goodput_full_kbps,flowmonitor_lost_packets";
  flows << "station,source_ip,dest_ip,dest_port," << metrics << "\n";
  auto writeMetrics = [&] (std::ostream &f, const IpCount &p, const AppCount &a)
    {
      NS_ABORT_MSG_IF (p.rx > p.tx, "Invalid IP counts");
      f << p.tx << ',' << p.rx << ',' << p.tx - p.rx << ','
        << NumberOrBlank (100.0 * p.rx, p.tx) << ','
        << NumberOrBlank (100.0 * (p.tx - p.rx), p.tx) << ','
        << NumberOrBlank (1000.0 * p.delay, p.rx) << ','
        << p.rxBytes << ',' << p.rxBytes * 8.0 / ((duration + cleanup) * 1000) << ','
        << a.txBytes << ',' << a.rxBytes << ',' << a.rxActive << ','
        << a.rxActive * 8.0 / (duration * 1000) << ','
        << a.rxBytes * 8.0 / ((duration + cleanup) * 1000) << ',' << p.lostMonitor;
    };
  IpCount total;
  AppCount appTotal;
  uint32_t zeroRx = 0, zeroTx = 0;
  positionFile << "station,x_start_m,y_start_m,x_end_m,y_end_m\n";
  for (uint32_t i = 0; i < stations; ++i)
    {
      const auto &p = perStation[i]; const auto &a = counts[i];
      flows << i << ',' << staIp.GetAddress (i) << ',' << apIp.GetAddress (0) << ',' << 9000+i << ',';
      writeMetrics (flows, p, a); flows << '\n';
      total.tx += p.tx; total.rx += p.rx; total.txBytes += p.txBytes;
      total.rxBytes += p.rxBytes; total.delay += p.delay; total.lostMonitor += p.lostMonitor;
      appTotal.txBytes += a.txBytes; appTotal.rxBytes += a.rxBytes; appTotal.rxActive += a.rxActive;
      zeroRx += (p.rx == 0); zeroTx += (p.tx == 0);
      Vector v = sta.Get (i)->GetObject<MobilityModel> ()->GetPosition ();
      positionFile << i << ',' << initial[i].x << ',' << initial[i].y << ',' << v.x << ',' << v.y << '\n';
    }
  summary << "schema_version,transport,tcp_variant,stations,seed,run,mobility,start_s,duration_s,cleanup_s,rate_mbps_per_sta,packet_size,half_side_m,speed_mps,tx_power_dbm,phy_mode," << metrics << ",zero_tx_flows,zero_rx_flows\n";
  summary << "1," << transport << ",NewReno," << stations << ',' << seed << ',' << run << ',' << mobility << ','
          << start << ',' << duration << ',' << cleanup << ',' << rateMbps << ',' << packetSize << ','
          << halfSide << ',' << speed << ',' << txPower << ",OfdmRate24Mbps,";
  writeMetrics (summary, total, appTotal);
  summary << ',' << zeroTx << ',' << zeroRx << '\n';
  flows.close (); summary.close (); positionFile.close ();
  NS_ABORT_MSG_IF (!flows || !summary || !positionFile, "Output write failure");
  if (xml) monitor->SerializeToXmlFile (output + "-flowmon.xml", false, false);
  std::cout << "DONE transport=" << transport << " stations=" << stations << " run=" << run
            << " txIP=" << total.tx << " rxIP=" << total.rx
            << " PDR_IP=" << NumberOrBlank (100.0 * total.rx, total.tx)
            << "% activeGoodput=" << appTotal.rxActive * 8.0 / (duration * 1000) << " Kbps\n";
  Simulator::Destroy ();
  return 0;
}
