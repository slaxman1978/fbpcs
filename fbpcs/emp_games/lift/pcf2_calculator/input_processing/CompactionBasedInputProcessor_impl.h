/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */

#pragma once

#include <algorithm>
#include <functional>
#include <iterator>
#include <numeric>
#include <stdexcept>
#include <tuple>
#include <vector>

#include "fbpcf/mpc_std_lib/util/secureRandomPermutation.h"
#include "fbpcs/emp_games/lift/pcf2_calculator/input_processing/IInputProcessor.h"

#include "fbpcs/emp_games/lift/pcf2_calculator/input_processing/CompactionBasedInputProcessor.h"

namespace private_lift {

template <int schedulerId>
std::vector<int32_t>
CompactionBasedInputProcessor<schedulerId>::shuffleAndGetUnionMap() {
  XLOG(INFO) << "Shuffling input and preparing Union Map for Adapter input";
  int32_t unionSize = inputData_.getNumRows();
  const std::vector<uint32_t> randomPermutation =
      fbpcf::mpc_std_lib::util::secureRandomPermutation(unionSize, *prg_);
  std::vector<int32_t> unionMap(unionSize);
  const std::vector<bool>& dummyRows = inputData_.getDummyRows();
  int32_t nonDummyRows = 0;
  for (int32_t i = 0; i < unionMap.size(); i++) {
    unionMap[randomPermutation[i]] =
        dummyRows[randomPermutation[i]] ? -1 : nonDummyRows++;
  }
  return unionMap;
}

template <int schedulerId>
std::vector<int32_t>
CompactionBasedInputProcessor<schedulerId>::getIntersectionMap(
    const std::vector<int32_t>& unionMap) {
  XLOG(INFO) << "Begin adapter protocol";
  return adapter_->adapt(unionMap);
}

template <int schedulerId>
std::vector<std::vector<unsigned char>>
CompactionBasedInputProcessor<schedulerId>::preparePlaintextData(
    const std::vector<int32_t>& unionMap) {
  std::vector<std::vector<unsigned char>> rst;
  XLOG(INFO) << "Begin plaintext data serialization as bytes";
  size_t unionSize = inputData_.getNumRows();
  int32_t inputSize = 0;
  std::vector<int32_t> reverseUnionMap(inputData_.getNumRows());

  for (int i = 0; i < unionMap.size(); i++) {
    if (unionMap[i] >= 0) {
      reverseUnionMap[unionMap[i]] = i;
      inputSize = std::max(inputSize, unionMap[i]);
    }
  }

  inputSize++;
  reverseUnionMap.resize(inputSize);

  rst.reserve(inputSize);

  if (myRole_ == common::PARTNER) {
    auto cohortIdsPadded =
        common::padArray<uint32_t>(inputData_.getGroupIds(), unionSize, 0);
    auto purchaseTimestampsPadded = common::padNestedArrays<uint32_t>(
        inputData_.getPurchaseTimestampArrays(),
        unionSize,
        numConversionsPerUser_,
        0);
    auto purchaseValuesPadded = common::padNestedArrays<int64_t>(
        inputData_.getPurchaseValueArrays(),
        unionSize,
        numConversionsPerUser_,
        0);
    auto purchaseValuesSquaredPadded = common::padNestedArrays<int64_t>(
        inputData_.getPurchaseValueSquaredArrays(),
        unionSize,
        numConversionsPerUser_,
        0);

    for (size_t i = 0; i < inputSize; i++) {
      int inputIndex = reverseUnionMap[i];

      bool anyValidPurchaseTimestamp = false;
      for (uint32_t purchaseTs : purchaseTimestampsPadded[inputIndex]) {
        // compute whether each row contains at least one valid (positive)
        // purchase timestamp
        anyValidPurchaseTimestamp |= (purchaseTs > 0);
      }

      PartnerRow partnerRow{
          .anyValidPurchaseTimestamp = anyValidPurchaseTimestamp,
          .cohortGroupId = cohortIdsPadded[inputIndex]};

      std::vector<PartnerConversionRow> rowConversions(numConversionsPerUser_);

      for (size_t j = 0; j < numConversionsPerUser_; j++) {
        rowConversions[j] = {
            .purchaseTimestamp = purchaseTimestampsPadded[inputIndex][j],
            .thresholdTimestamp = purchaseTimestampsPadded[inputIndex][j] > 0
                ? purchaseTimestampsPadded[inputIndex][j] +
                    kPurchaseTimestampThresholdWindow
                : 0,
            .purchaseValue = (int32_t)purchaseValuesPadded[inputIndex][j],
            .purchaseValueSquared = purchaseValuesSquaredPadded[inputIndex][j]};
      }

      std::vector<unsigned char> serialized(
          PARTNER_ROW_SIZE_BYTES +
          PARTNER_CONVERSION_ROW_SIZE_BYTES * numConversionsPerUser_);

      serialized[0] = partnerRow.anyValidPurchaseTimestamp;
      serialized[1] = extractByte(partnerRow.cohortGroupId, 0);
      serialized[2] = extractByte(partnerRow.cohortGroupId, 1);
      serialized[3] = extractByte(partnerRow.cohortGroupId, 2);
      serialized[4] = extractByte(partnerRow.cohortGroupId, 3);

      for (size_t j = 0; j < numConversionsPerUser_; j++) {
        for (int byte = 0; byte < 4; byte++) {
          serialized[5 + j * PARTNER_CONVERSION_ROW_SIZE_BYTES + byte] =
              extractByte(rowConversions[j].purchaseTimestamp, byte);
          serialized[9 + j * PARTNER_CONVERSION_ROW_SIZE_BYTES + byte] =
              extractByte(rowConversions[j].thresholdTimestamp, byte);
          serialized[13 + j * PARTNER_CONVERSION_ROW_SIZE_BYTES + byte] =
              extractByte(rowConversions[j].purchaseValue, byte);
        }

        for (size_t byte = 0; byte < 8; byte++) {
          serialized[17 + j * PARTNER_CONVERSION_ROW_SIZE_BYTES + byte] =
              extractByte(rowConversions[j].purchaseValueSquared, byte);
        }
      }

      rst.push_back(serialized);
    }
  } else {
    auto opportunityTimestampsPadded = common::padArray<uint32_t>(
        inputData_.getOpportunityTimestamps(), unionSize, 0);
    auto controlPopulationPadded = common::padArray<bool>(
        inputData_.getControlPopulation(), unionSize, false);
    auto testPopulationPadded = common::padArray<bool>(
        inputData_.getTestPopulation(), unionSize, false);
    auto numImpressionsPadded =
        common::padArray<int64_t>(inputData_.getNumImpressions(), unionSize, 0);
    auto breakdownIdPadded =
        common::padArray<uint32_t>(inputData_.getBreakdownIds(), unionSize, 0);
    for (size_t i = 0; i < inputSize; i++) {
      int inputIndex = reverseUnionMap[i];

      bool isValidOpportunityTimestamp =
          (opportunityTimestampsPadded.at(inputIndex) > 0) &
          (controlPopulationPadded.at(inputIndex) |
           testPopulationPadded.at(inputIndex));

      bool testReach = testPopulationPadded.at(inputIndex) &
          (numImpressionsPadded.at(inputIndex) > 0);

      PublisherRow publisherRow{
          .breakdownId = (bool)breakdownIdPadded[inputIndex],
          .controlPopulation = controlPopulationPadded[inputIndex],
          .isValidOpportunityTimestamp = isValidOpportunityTimestamp,
          .testReach = testReach,
          .opportunityTimestamp = opportunityTimestampsPadded[inputIndex],
      };

      std::vector<unsigned char> serialized(PUBLISHER_ROW_BYTES);
      serialized[0] = publisherRow.breakdownId |
          (publisherRow.controlPopulation << 1) |
          (publisherRow.isValidOpportunityTimestamp << 2) |
          (publisherRow.testReach << 3);

      for (size_t byte = 0; byte < 4; byte++) {
        serialized[1 + byte] =
            extractByte(publisherRow.opportunityTimestamp, byte);
      }

      rst.push_back(serialized);
    }
  }

  return rst;
}

template <int schedulerId>
std::pair<
    typename CompactionBasedInputProcessor<schedulerId>::SecString,
    typename CompactionBasedInputProcessor<schedulerId>::SecString>
CompactionBasedInputProcessor<schedulerId>::compactData(
    const std::vector<int32_t>& intersectionMap,
    const std::vector<std::vector<unsigned char>>& plaintextData) {
  XLOG(INFO) << "Beginning oblivious data intersection step";

  int32_t myRows = plaintextData.size();

  auto publisherRows = common::shareIntFrom<
      schedulerId,
      sizeof(myRows) * 8,
      common::PUBLISHER,
      common::PARTNER>(myRole_, myRows);

  auto partnerRows = common::shareIntFrom<
      schedulerId,
      sizeof(myRows) * 8,
      common::PARTNER,
      common::PUBLISHER>(myRole_, myRows);

  XLOG(INFO) << "Publisher Row count: " << publisherRows;
  XLOG(INFO) << "Publisher Row size in bytes: " << PUBLISHER_ROW_BYTES;

  XLOG(INFO) << "Partner Row count: " << partnerRows;
  XLOG(INFO) << "Partner Row size in bytes: "
             << PARTNER_CONVERSION_ROW_SIZE_BYTES * numConversionsPerUser_ +
          PARTNER_ROW_SIZE_BYTES;

  SecString publisherDataShares;
  SecString partnerDataShares;

  if (myRole_ == common::PUBLISHER) {
    XLOG(INFO) << "Begin processing my data (publisher)";
    publisherDataShares =
        dataProcessor_->processMyData(plaintextData, intersectionMap.size());
    XLOG(INFO) << "Begin processing peers data (partner)";
    partnerDataShares = dataProcessor_->processPeersData(
        partnerRows,
        intersectionMap,
        PARTNER_CONVERSION_ROW_SIZE_BYTES * numConversionsPerUser_ +
            PARTNER_ROW_SIZE_BYTES);
  } else if (myRole_ == common::PARTNER) {
    XLOG(INFO) << "Begin processing peers data (publisher)";
    publisherDataShares = dataProcessor_->processPeersData(
        publisherRows, intersectionMap, PUBLISHER_ROW_BYTES);
    XLOG(INFO) << "Begin processing my data (partner)";
    partnerDataShares =
        dataProcessor_->processMyData(plaintextData, intersectionMap.size());
  }

  auto expectedIntersectionSize = std::transform_reduce(
      intersectionMap.begin(),
      intersectionMap.end(),
      0,
      [](const int32_t& left, const int32_t& right) { return left + right; },
      [](const int32_t& ele) { return ele == -1 ? 0 : 1; });

  if (expectedIntersectionSize != publisherDataShares.getBatchSize()) {
    throw std::runtime_error(folly::sformat(
        "Publisher rows do not match up expected intersection size. Expected {} but got {} rows.",
        expectedIntersectionSize,
        publisherDataShares.getBatchSize()));
  }

  if (expectedIntersectionSize != partnerDataShares.getBatchSize()) {
    throw std::runtime_error(folly::sformat(
        "Partner rows do not match up expected intersection size. Expected {} but got {} rows.",
        expectedIntersectionSize,
        partnerDataShares.getBatchSize()));
  }

  XLOG(INFO) << folly::format(
      "{} rows in intersection after running data processor",
      expectedIntersectionSize);

  return std::make_pair<
      typename CompactionBasedInputProcessor<schedulerId>::SecString,
      typename CompactionBasedInputProcessor<schedulerId>::SecString>(
      std::move(publisherDataShares), std::move(partnerDataShares));
}

template <int schedulerId>
void CompactionBasedInputProcessor<schedulerId>::extractCompactedData(
    const typename CompactionBasedInputProcessor<schedulerId>::SecString&
        publisherDataShares,
    const typename CompactionBasedInputProcessor<schedulerId>::SecString&
        partnerDataShares) {
  XLOG(INFO, "Begin extraction to MPC types");

  liftGameProcessedData_.numRows = publisherDataShares.getBatchSize();

  std::tuple<
      std::vector<PartnerRow>,
      std::vector<std::vector<PartnerConversionRow>>,
      std::vector<PublisherRow>>
      deserializedSecretStructs =
          deserializeSecretSharedData(publisherDataShares, partnerDataShares);

  extractPartnerValues(std::get<0>(deserializedSecretStructs));
  extractPartnerConversionValues(std::get<1>(deserializedSecretStructs));
  extractPublisherValues(std::get<2>(deserializedSecretStructs));

  XLOG(INFO, "Finish extraction to MPC types");
}

template <int schedulerId>
std::tuple<
    std::vector<
        typename CompactionBasedInputProcessor<schedulerId>::PartnerRow>,
    std::vector<std::vector<typename CompactionBasedInputProcessor<
        schedulerId>::PartnerConversionRow>>,
    std::vector<
        typename CompactionBasedInputProcessor<schedulerId>::PublisherRow>>
CompactionBasedInputProcessor<schedulerId>::deserializeSecretSharedData(
    const SecString& publisherDataShares,
    const SecString& partnerDataShares) {
  std::vector<std::vector<bool>> publisherSecretSharedBits =
      publisherDataShares.extractStringShare().getValue();
  publisherSecretSharedBits = common::transpose(publisherSecretSharedBits);

  std::vector<std::vector<bool>> partnerSecretSharedBits =
      partnerDataShares.extractStringShare().getValue();
  partnerSecretSharedBits = common::transpose(partnerSecretSharedBits);

  std::vector<std::vector<PartnerConversionRow>> partnerConversionRows(
      liftGameProcessedData_.numRows);
  std::vector<PartnerRow> partnerRows(liftGameProcessedData_.numRows);
  std::vector<PublisherRow> publisherRows(liftGameProcessedData_.numRows);

  for (size_t i = 0; i < liftGameProcessedData_.numRows; i++) {
    auto publisherByteShares = convertFromBits(publisherSecretSharedBits.at(i));
    auto partnerByteShares = convertFromBits(partnerSecretSharedBits.at(i));

    publisherRows[i].breakdownId = publisherByteShares[0] & 1;
    publisherRows[i].controlPopulation = (publisherByteShares[0] >> 1) & 1;
    publisherRows[i].isValidOpportunityTimestamp =
        (publisherByteShares[0] >> 2) & 1;
    publisherRows[i].testReach = (publisherByteShares[0] >> 3) & 1;
    publisherRows[i].opportunityTimestamp =
        reconstructFromBytes<uint32_t>(publisherByteShares.data() + 1);

    partnerRows[i].anyValidPurchaseTimestamp = partnerByteShares[0] & 1;
    partnerRows[i].cohortGroupId =
        reconstructFromBytes<uint32_t>(partnerByteShares.data() + 1);

    partnerConversionRows[i] =
        std::vector<PartnerConversionRow>(numConversionsPerUser_);

    for (size_t j = 0; j < numConversionsPerUser_; j++) {
      partnerConversionRows[i][j].purchaseTimestamp =
          reconstructFromBytes<uint32_t>(
              partnerByteShares.data() + 5 +
              j * PARTNER_CONVERSION_ROW_SIZE_BYTES);
      partnerConversionRows[i][j].thresholdTimestamp =
          reconstructFromBytes<uint32_t>(
              partnerByteShares.data() + 9 +
              j * PARTNER_CONVERSION_ROW_SIZE_BYTES);

      partnerConversionRows[i][j].purchaseValue = reconstructFromBytes<int32_t>(
          partnerByteShares.data() + 13 +
          j * PARTNER_CONVERSION_ROW_SIZE_BYTES);

      partnerConversionRows[i][j].purchaseValueSquared =
          reconstructFromBytes<int64_t>(
              partnerByteShares.data() + 17 +
              j * PARTNER_CONVERSION_ROW_SIZE_BYTES);
    }
  }

  return std::make_tuple<
      std::vector<
          typename CompactionBasedInputProcessor<schedulerId>::PartnerRow>,
      std::vector<std::vector<typename CompactionBasedInputProcessor<
          schedulerId>::PartnerConversionRow>>,
      std::vector<
          typename CompactionBasedInputProcessor<schedulerId>::PublisherRow>>(
      std::move(partnerRows),
      std::move(partnerConversionRows),
      std::move(publisherRows));
}

template <int schedulerId>
void CompactionBasedInputProcessor<schedulerId>::extractPartnerValues(
    const std::vector<PartnerRow>& partnerRows) {
  std::vector<bool> anyValidPurchaseTimestampShares(
      liftGameProcessedData_.numRows);
  std::vector<uint64_t> groupIdShares(liftGameProcessedData_.numRows);

  for (int row = 0; row < liftGameProcessedData_.numRows; row++) {
    anyValidPurchaseTimestampShares[row] =
        partnerRows[row].anyValidPurchaseTimestamp;
    groupIdShares[row] = partnerRows[row].cohortGroupId;
  }

  liftGameProcessedData_.anyValidPurchaseTimestamp =
      SecBit<schedulerId>(typename SecBit<schedulerId>::ExtractedBit(
          anyValidPurchaseTimestampShares));

  cohortGroupIds_ = SecGroup<schedulerId>(
      typename SecGroup<schedulerId>::ExtractedInt(groupIdShares));
}

template <int schedulerId>
void CompactionBasedInputProcessor<schedulerId>::extractPartnerConversionValues(
    const std::vector<std::vector<PartnerConversionRow>>&
        partnerConversionRows) {
  liftGameProcessedData_.purchaseTimestamps =
      std::vector<SecTimestamp<schedulerId>>(numConversionsPerUser_);
  liftGameProcessedData_.thresholdTimestamps =
      std::vector<SecTimestamp<schedulerId>>(numConversionsPerUser_);
  liftGameProcessedData_.purchaseValues =
      std::vector<SecValue<schedulerId>>(numConversionsPerUser_);
  liftGameProcessedData_.purchaseValueSquared =
      std::vector<SecValueSquared<schedulerId>>(numConversionsPerUser_);

  for (int conversion = 0; conversion < numConversionsPerUser_; conversion++) {
    std::vector<uint64_t> purchaseTimestampShares(
        liftGameProcessedData_.numRows);
    std::vector<uint64_t> thresholdTimestampShares(
        liftGameProcessedData_.numRows);
    std::vector<int64_t> purchaseValueShares(liftGameProcessedData_.numRows);
    std::vector<int64_t> purchaseValueSquaredShares(
        liftGameProcessedData_.numRows);

    for (int row = 0; row < liftGameProcessedData_.numRows; row++) {
      purchaseTimestampShares[row] =
          partnerConversionRows[row][conversion].purchaseTimestamp;
      thresholdTimestampShares[row] =
          partnerConversionRows[row][conversion].thresholdTimestamp;
      purchaseValueShares[row] =
          partnerConversionRows[row][conversion].purchaseValue;
      purchaseValueSquaredShares[row] =
          partnerConversionRows[row][conversion].purchaseValueSquared;
    }

    liftGameProcessedData_.purchaseTimestamps[conversion] =
        SecTimestamp<schedulerId>(
            typename SecTimestamp<schedulerId>::ExtractedInt(
                purchaseTimestampShares));
    liftGameProcessedData_.thresholdTimestamps[conversion] =
        SecTimestamp<schedulerId>(
            typename SecTimestamp<schedulerId>::ExtractedInt(
                thresholdTimestampShares));
    liftGameProcessedData_.purchaseValues[conversion] = SecValue<schedulerId>(
        typename SecValue<schedulerId>::ExtractedInt(purchaseValueShares));

    liftGameProcessedData_.purchaseValueSquared[conversion] =
        SecValueSquared<schedulerId>(
            typename SecValueSquared<schedulerId>::ExtractedInt(
                purchaseValueSquaredShares));
  }
}

template <int schedulerId>
void CompactionBasedInputProcessor<schedulerId>::extractPublisherValues(
    const std::vector<PublisherRow>& publisherRows) {
  std::vector<bool> breakdownGroupIdShares(liftGameProcessedData_.numRows);
  std::vector<bool> controlPopulationShares(liftGameProcessedData_.numRows);
  std::vector<bool> isValidOpportunityTimestampShares(
      liftGameProcessedData_.numRows);
  std::vector<bool> testReachShares(liftGameProcessedData_.numRows);
  std::vector<uint64_t> opportunityTimestampShares(
      liftGameProcessedData_.numRows);

  for (int row = 0; row < liftGameProcessedData_.numRows; row++) {
    breakdownGroupIdShares[row] = publisherRows[row].breakdownId;
    controlPopulationShares[row] = publisherRows[row].controlPopulation;
    isValidOpportunityTimestampShares[row] =
        publisherRows[row].isValidOpportunityTimestamp;
    testReachShares[row] = publisherRows[row].testReach;
    opportunityTimestampShares[row] = publisherRows[row].opportunityTimestamp;
  }

  breakdownGroupIds_ = SecBit<schedulerId>(
      typename SecBit<schedulerId>::ExtractedBit(breakdownGroupIdShares));
  controlPopulation_ = SecBit<schedulerId>(
      typename SecBit<schedulerId>::ExtractedBit(controlPopulationShares));
  liftGameProcessedData_.isValidOpportunityTimestamp =
      SecBit<schedulerId>(typename SecBit<schedulerId>::ExtractedBit(
          isValidOpportunityTimestampShares));
  liftGameProcessedData_.testReach = SecBit<schedulerId>(
      typename SecBit<schedulerId>::ExtractedBit(testReachShares));
  liftGameProcessedData_.opportunityTimestamps = SecTimestamp<schedulerId>(
      typename SecTimestamp<schedulerId>::ExtractedInt(
          opportunityTimestampShares));
}
} // namespace private_lift
